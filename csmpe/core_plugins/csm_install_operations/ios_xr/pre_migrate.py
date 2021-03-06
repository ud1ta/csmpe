# =============================================================================
# pre_migrate.py - plugin for preparing for migrating classic XR to eXR/fleXR
#
# Copyright (c)  2013, Cisco Systems
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# Redistributions of source code must retain the above copyright notice,
# this list of conditions and the following disclaimer.
# Redistributions in binary form must reproduce the above copyright notice,
# this list of conditions and the following disclaimer in the documentation
# and/or other materials provided with the distribution.
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF
# THE POSSIBILITY OF SUCH DAMAGE.
# =============================================================================

import csv
import os
import re
import subprocess
import json

import pexpect

from csmpe.plugins import CSMPlugin
from csmpe.context import PluginError
from utils import ServerType, is_empty, concatenate_dirs
from simple_server_helper import TFTPServer, FTPServer, SFTPServer
from csmpe.core_plugins.csm_node_status_check.ios_xr.plugin import Plugin as NodeStatusPlugin
from add import Plugin as InstallAddPlugin
from activate import Plugin as InstallActivatePlugin
from commit import Plugin as InstallCommitPlugin
from migration_lib import SUPPORTED_HW_JSON

MINIMUM_RELEASE_VERSION_FOR_MIGRATION = "5.3.3"
RELEASE_VERSION_DOES_NOT_NEED_FPD_SMU = "6.1.1"

NOX_64_BINARY = "nox-linux-64.bin"
# NOX_32_BINARY = "nox_linux_32bit_6.0.0v3.bin"
NOX_FOR_MAC = "nox-mac64"

TIMEOUT_FOR_COPY_CONFIG = 3600
TIMEOUT_FOR_COPY_IMAGE = 960
TIMEOUT_FOR_FPD_UPGRADE = 9600

IMAGE_LOCATION = "harddiskb:/"

ROUTEPROCESSOR_RE = '\d+/RS??P\d+/CPU\d+'
LINECARD_RE = '\d+/\d+/CPU\d+'
# SUPPORTED_CARDS = ['4X100', '8X100', '12X100']
NODE = '(\d+/(?:RS?P)?\d+/CPU\d+)'
FAN = '\d+/FT\d+/SP'
PEM = '\d+/P[SM]\d+/M?\d+/SP'

FPDS_CHECK_FOR_UPGRADE = set(['cbc', 'rommon', 'fpga2', 'fsbl', 'lnxfw', 'fpga8', 'fclnxfw', 'fcfsbl'])

XR_CONFIG_IN_CSM = "xr.cfg"
BREAKOUT_CONFIG_IN_CSM = "breakout.cfg"
ADMIN_CONFIG_IN_CSM = "admin.cfg"

CONVERTED_XR_CONFIG_IN_CSM = "xr.iox"
CONVERTED_ADMIN_CAL_CONFIG_IN_CSM = "admin.cal"
CONVERTED_ADMIN_XR_CONFIG_IN_CSM = "admin.iox"

XR_CONFIG_ON_DEVICE = "iosxr.cfg"
ADMIN_CAL_CONFIG_ON_DEVICE = "admin_calvados.cfg"
ADMIN_XR_CONFIG_ON_DEVICE = "admin_iosxr.cfg"

VALID_STATE = ['IOS XR RUN',
               'PRESENT',
               'READY',
               'OK']


class Plugin(CSMPlugin):
    """
    A plugin for preparing device for migration from
    ASR9K IOS-XR (a.k.a. XR) to ASR9K IOS-XR 64 bit (a.k.a. eXR)

    This plugin does the following:
    1. Check several pre-requisites
    2. Resize the eUSB partition(/harddiskb:/ on XR)
    3. Migrate the configurations with NoX and upload them to device
    4. Copy the eXR image to /harddiskb:/
    5. Upgrade some FPD's if needed.

    Console access is needed.
    """
    name = "Pre-Migrate Plugin"
    platforms = {'ASR9K'}
    phases = {'Pre-Migrate'}

    def _check_if_rp_fan_pem_supported_and_in_valid_state(self, supported_hw):
        """Check if all RSP/RP/FAN/PEM currently on device are supported and are in valid state for migration."""
        cmd = "show platform"
        output = self.ctx.send(cmd)
        file_name = self.ctx.save_to_file(cmd, output)
        if file_name is None:
            self.ctx.warning("Unable to save '{}' output to file: {}".format(cmd, file_name))

        inventory = self.ctx.load_data("inventory")

        rp_pattern = re.compile(ROUTEPROCESSOR_RE)
        fan_pattern = re.compile(FAN)
        pem_pattern = re.compile(PEM)

        for key, value in inventory.items():

            rp_or_rsp = self._check_if_supported_and_in_valid_state(key, rp_pattern, value, supported_hw.get("RP"))
            if not rp_or_rsp:
                fan = self._check_if_supported_and_in_valid_state(key, fan_pattern, value, supported_hw.get("FAN"))
                if not fan:
                    self._check_if_supported_and_in_valid_state(key, pem_pattern, value, supported_hw.get("PEM"))

        return True

    def _check_if_supported_and_in_valid_state(self, node_name, card_pattern, value, supported_type_list):
        """
        Check if a card (RSP/RP/FAN/PEM) is supported and in valid state.
        :param node_name: the name under "Node" column in output of CLI "show platform". i.e., "0/RSP0/CPU0"
        :param card_pattern: the regex for either the node name of a RSP, RP, FAN or PEM
        :param value: the inventory value for nodes - through parsing output of "show platform"
        :param supported_type_list: the list of card types/pids that are supported for migration
        :return: True if this node is indeed the asked card(RP/RSP/FAN/PEM) and it's confirmed that it's supported
                    for migration.
                False if this node is not the asked card(RP/RSP/FAN/PEM).
                error out if this node is indeed the asked card(RP/RSP/FAN/PEM) and it is NOT supported for migration.
        """
        if card_pattern.match(node_name):
            supported = False
            if value['state'] not in VALID_STATE:
                    self.ctx.error("{}={}: {}".format(node_name, value, "Not in valid state for migration"))
            if not supported_type_list:
                self.ctx.error("The supported hardware list is missing information.")
            for supported_type in supported_type_list:
                if supported_type in value['type']:
                    supported = True
                    break
            if not supported:
                self.ctx.error("The card type for {} is not supported for migration to ASR9K-X64.".format(node_name) +
                               " Please check the user manuel under 'Help' on CSM Server for list of " +
                               "supported hardware.")
            return True
        return False

    def _get_supported_iosxr_run_nodes(self, supported_hw):
        """Get names of all RSP's, RP's and Linecards in IOS-XR RUN state that are supported for migration."""
        inventory = self.ctx.load_data("inventory")

        supported_iosxr_run_nodes = []

        node_pattern = re.compile(NODE)
        if supported_hw.get("RP") and supported_hw.get("LC"):
            supported_cards = supported_hw.get("RP") + supported_hw.get("LC")
        else:
            self.ctx.error("The supported hardware list is missing information on RP and/or LC.")

        for key, value in inventory.items():
            if node_pattern.match(key):
                if value['state'] == 'IOS XR RUN':
                    for card in supported_cards:
                        if card in value['type']:
                            supported_iosxr_run_nodes.append(key)
                            break
        return supported_iosxr_run_nodes

    def _ping_repository_check(self, repo_url):
        """Test ping server repository ip from device"""
        repo_ip = re.search("[/@](\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})/", repo_url)

        if not repo_ip:
            self.ctx.error("Bad hostname for server repository. Please check the settings in CSM.")

        output = self.ctx.send("ping {}".format(repo_ip.group(1)))
        if "100 percent" not in output:
            self.ctx.error("Failed to ping server repository {} on device." +
                           "Please check session.log.".format(repo_ip.group(1)))

    def _all_configs_supported(self, nox_output):
        """Check text output from running NoX on system. Only return True if all configs are supported by eXR."""
        pattern = "Filename[\sA-Za-z\n]*[-\s]*\S*\s+\d*\s+\d*\(\s*\d*%\)\s+\d*\(\s*\d*%\)\s+\d*\(\s*\d*%\)\s+(\d*)"
        match = re.search(pattern, nox_output)

        if match:
            if match.group(1) != "0":
                return False

        return True

    def _upload_files_to_server_repository(self, sourcefiles, server, destfilenames):
        """
        Upload files from their locations in the host linux system to the FTP/TFTP/SFTP server repository.

        Arguments:
        :param sourcefiles: a list of string file paths that each points to a file on the system where CSM is hosted.
                            The paths are all relative to csm/csmserver/.
                            For example, if the source file is in csm_data/migration/filename,
                            sourcefiles = ["../../csm_data/migration/filename"]
        :param server: the associated server repository object stored in CSM database
        :param destfilenames: a list of string filenames that the source files should be named after being copied to
                              the designated directory in the server repository. i.e., ["thenewfilename"]
        :return: True if no error occurred.
        """

        server_type = server.server_type
        if server_type == ServerType.TFTP_SERVER:
            tftp_server = TFTPServer(server)
            for x in range(0, len(sourcefiles)):
                self.ctx.info("Coping file {} to {}/{}/{}.".format(sourcefiles[x],
                                                                   server.server_directory,
                                                                   self.ctx._csm.install_job.server_directory,
                                                                   destfilenames[x]))
                try:
                    tftp_server.upload_file(sourcefiles[x], destfilenames[x],
                                            sub_directory=self.ctx._csm.install_job.server_directory)
                except:
                    self.ctx.error("Exception was thrown while " +
                                   "copying file {} to {}/{}/{}.".format(sourcefiles[x],
                                                                         server.server_directory,
                                                                         self.ctx._csm.install_job.server_directory,
                                                                         destfilenames[x]))

        elif server_type == ServerType.FTP_SERVER:
            ftp_server = FTPServer(server)
            for x in range(0, len(sourcefiles)):
                self.ctx.info("Coping file {} to {}/{}/{}.".format(sourcefiles[x],
                                                                   server.server_directory,
                                                                   self.ctx._csm.install_job.server_directory,
                                                                   destfilenames[x]))
                try:
                    ftp_server.upload_file(sourcefiles[x], destfilenames[x],
                                           sub_directory=self.ctx._csm.install_job.server_directory)
                except:
                    self.ctx.error("Exception was thrown while " +
                                   "copying file {} to {}/{}/{}.".format(sourcefiles[x],
                                                                         server.server_directory,
                                                                         self.ctx._csm.install_job.server_directory,
                                                                         destfilenames[x]))
        elif server_type == ServerType.SFTP_SERVER:
            sftp_server = SFTPServer(server)
            for x in range(0, len(sourcefiles)):
                self.ctx.info("Coping file {} to {}/{}/{}.".format(sourcefiles[x],
                                                                   server.server_directory,
                                                                   self.ctx._csm.install_job.server_directory,
                                                                   destfilenames[x]))
                try:
                    sftp_server.upload_file(sourcefiles[x], destfilenames[x],
                                            sub_directory=self.ctx._csm.install_job.server_directory)
                except:
                    self.ctx.error("Exception was thrown while " +
                                   "copying file {} to {}/{}/{}.".format(sourcefiles[x],
                                                                         server.server_directory,
                                                                         self.ctx._csm.install_job.server_directory,
                                                                         destfilenames[x]))
        else:
            self.ctx.error("Pre-Migrate does not support {} server repository.".format(server_type))

        return True

    def _copy_files_to_device(self, server, repository, source_filenames, dest_files, timeout=600):
        """
        Copy files from their locations in the user selected server directory in the FTP/TFTP/SFTP server repository
        to locations on device.

        Arguments:
        :param server: the server object fetched from database
        :param repository: the string url link that points to the location of files in the SFTP server repository
        :param source_filenames: a list of string filenames in the designated directory in the server repository.
        :param dest_files: a list of string file paths that each points to a file to be created on device.
                    i.e., ["harddiskb:/asr9k-mini-x64.tar"]
        :param timeout: the timeout for the sftp copy operation on device. The default is 10 minutes.
        :return: None if no error occurred.
        """

        if server.server_type == ServerType.FTP_SERVER or server.server_type == ServerType.TFTP_SERVER:
            self._copy_files_from_ftp_tftp_to_device(repository, source_filenames, dest_files, timeout=timeout)

        elif server.server_type == ServerType.SFTP_SERVER:
            self._copy_files_from_sftp_to_device(server, source_filenames, dest_files, timeout=timeout)

        else:
            self.ctx.error("Pre-Migrate does not support {} server repository.".format(server.server_type))

    def _copy_files_from_ftp_tftp_to_device(self, repository, source_filenames, dest_files, timeout=600):
        """
        Copy files from their locations in the user selected server directory in the FTP or TFTP server repository
        to locations on device.

        Arguments:
        :param repository: the string url link that points to the location of files in the FTP/TFTP server repository,
                    with no extra '/' in the end. i.e., tftp://223.255.254.245/tftpboot
        :param source_filenames: a list of string filenames in the designated directory in the server repository.
        :param dest_files: a list of string file paths that each points to a file to be created on device.
                    i.e., ["harddiskb:/asr9k-mini-x64.tar"]
        :param timeout: the timeout for the 'copy' CLI command on device. The default is 10 minutes.
        :return: None if no error occurred.
        """

        def send_newline(ctx):
            ctx.ctrl.sendline()
            return True

        def error(ctx):
            ctx.message = "Error copying file."
            return False

        for x in range(0, len(source_filenames)):

            command = "copy {}/{} {}".format(repository, source_filenames[x], dest_files[x])

            CONFIRM_FILENAME = re.compile("Destination filename.*\?")
            CONFIRM_OVERWRITE = re.compile("Copy : Destination exists, overwrite \?\[confirm\]")
            COPIED = re.compile(".+bytes copied in.+ sec")
            NO_SUCH_FILE = re.compile("%Error copying.*\(Error opening source file\): No such file or directory")
            ERROR_COPYING = re.compile("%Error copying")

            PROMPT = self.ctx.prompt
            TIMEOUT = self.ctx.TIMEOUT

            events = [PROMPT, CONFIRM_FILENAME, CONFIRM_OVERWRITE, COPIED, TIMEOUT, NO_SUCH_FILE, ERROR_COPYING]
            transitions = [
                (CONFIRM_FILENAME, [0], 1, send_newline, timeout),
                (CONFIRM_OVERWRITE, [1], 2, send_newline, timeout),
                (COPIED, [1, 2], 3, None, 20),
                (PROMPT, [3], -1, None, 0),
                (TIMEOUT, [0, 1, 2, 3], -1, error, 0),
                (NO_SUCH_FILE, [0, 1, 2, 3], -1, error, 0),
                (ERROR_COPYING, [0, 1, 2, 3], -1, error, 0),
            ]

            self.ctx.info("Copying {}/{} to {} on device".format(repository,
                                                                 source_filenames[x],
                                                                 dest_files[x]))

            if not self.ctx.run_fsm("Copy file from tftp/ftp to device", command, events, transitions, timeout=20):
                self.ctx.error("Error copying {}/{} to {} on device".format(repository,
                                                                            source_filenames[x],
                                                                            dest_files[x]))

            output = self.ctx.send("dir {}".format(dest_files[x]))
            if "No such file" in output:
                self.ctx.error("Failed to copy {}/{} to {} on device".format(repository,
                                                                             source_filenames[x],
                                                                             dest_files[x]))

    def _copy_files_from_sftp_to_device(self, server, source_filenames, dest_files, timeout=600):
        """
        Copy files from their locations in the user selected server directory in the SFTP server repository
        to locations on device.

        Arguments:
        :param server: the sftp server object
        :param source_filenames: a list of string filenames in the designated directory in the server repository.
        :param dest_files: a list of string file paths that each points to a file to be created on device.
                    i.e., ["harddiskb:/asr9k-mini-x64.tar"]
        :param timeout: the timeout for the sftp copy operation on device. The default is 10 minutes.
        :return: None if no error occurred.
        """
        source_path = server.server_url

        remote_directory = concatenate_dirs(server.server_directory, self.ctx._csm.install_job.server_directory)
        if not is_empty(remote_directory):
            source_path = source_path + ":{}".format(remote_directory)

        def send_password(ctx):
            ctx.ctrl.sendline(server.password)
            if ctx.ctrl._session.logfile_read:
                ctx.ctrl._session.logfile_read = None
            return True

        def send_yes(ctx):
            ctx.ctrl.sendline("yes")
            if ctx.ctrl._session.logfile_read:
                ctx.ctrl._session.logfile_read = None
            return True

        def reinstall_logfile(ctx):
            if self.ctx._connection._session_fd and (not ctx.ctrl._session.logfile_read):
                ctx.ctrl._session.logfile_read = self.ctx._connection._session_fd
            else:
                ctx.message = "Error reinstalling session.log."
                return False
            return True

        def error(ctx):
            if self.ctx._connection._session_fd and (not ctx.ctrl._session.logfile_read):
                ctx.ctrl._session.logfile_read = self.ctx._connection._session_fd
            ctx.message = "Error copying file."
            return False

        for x in range(0, len(source_filenames)):
            if is_empty(server.vrf):
                command = "sftp {}@{}/{} {}".format(server.username, source_path, source_filenames[x], dest_files[x])
            else:
                command = "sftp {}@{}/{} {} vrf {}".format(server.username, source_path, source_filenames[x],
                                                           dest_files[x], server.vrf)

            PASSWORD = re.compile("Password:")
            CONFIRM_OVERWRITE = re.compile("Overwrite.*continue\? \[yes/no\]:")
            COPIED = re.compile("bytes copied in", re.MULTILINE)
            NO_SUCH_FILE = re.compile("src.*does not exist")
            DOWNLOAD_ABORTED = re.compile("Download aborted.")

            PROMPT = self.ctx.prompt
            TIMEOUT = self.ctx.TIMEOUT

            events = [PROMPT, PASSWORD, CONFIRM_OVERWRITE, COPIED, TIMEOUT, NO_SUCH_FILE, DOWNLOAD_ABORTED]
            transitions = [
                (PASSWORD, [0], 1, send_password, timeout),
                (CONFIRM_OVERWRITE, [1], 2, send_yes, timeout),
                (COPIED, [1, 2], -1, reinstall_logfile, 0),
                (PROMPT, [1, 2], -1, reinstall_logfile, 0),
                (TIMEOUT, [0, 1, 2], -1, error, 0),
                (NO_SUCH_FILE, [0, 1, 2], -1, error, 0),
                (DOWNLOAD_ABORTED, [0, 1, 2], -1, error, 0),
            ]

            self.ctx.info("Copying {}/{} to {} on device".format(source_path,
                                                                 source_filenames[x],
                                                                 dest_files[x]))

            if not self.ctx.run_fsm("Copy file from sftp to device", command, events, transitions, timeout=20):
                self.ctx.error("Error copying {}/{} to {} on device".format(source_path,
                                                                            source_filenames[x],
                                                                            dest_files[x]))

            if self.ctx._connection._session_fd and (not self.ctx._connection._driver.ctrl._session.logfile_read):
                self.ctx._connection._driver.ctrl._session.logfile_read = self.ctx._connection._session_fd
            output = self.ctx.send("dir {}".format(dest_files[x]))
            if "No such file" in output:
                self.ctx.error("Failed to copy {}/{} to {} on device".format(source_path,
                                                                             source_filenames[x],
                                                                             dest_files[x]))

    def _run_migration_on_config(self, fileloc, filename, nox_to_use, hostname):
        """
        Run the migration tool - NoX - on the configurations copied out from device.

        The conversion/migration is successful if the number under 'Total' equals to
        the number under 'Known' in the text output.

        If it's successful, but not all existing configs are supported by eXR, create two
        new log files for the supported and unsupported configs in session log directory.
        The unsupported configs will not appear on the converted configuration files.
        Log a warning about the removal of unsupported configs, but this is not considered
        as error.

        If it's not successful, meaning that there are some configurations not known to
        the NoX tool, in this case, create two new log files for the supported and unsupported
        configs in session log directory. After that, error out.

        :param fileloc: string location where the config needs to be converted/migrated is,
                        without the '/' in the end. This location is relative to csm/csmserver/
        :param filename: string filename of the config
        :param nox_to_use: string name of NoX binary executable.
        :param hostname: hostname of device, as recorded on CSM.
        :return: None if no error occurred.
        """

        try:
            commands = [subprocess.Popen(["chmod", "+x", nox_to_use]),
                        subprocess.Popen([nox_to_use, "-f", os.path.join(fileloc, filename)],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                        ]

            nox_output, nox_error = commands[1].communicate()
        except OSError:
            self.ctx.error("Failed to run the configuration migration tool {} on config file {} - OSError.".format(
                nox_to_use,
                os.path.join(fileloc, filename))
            )

        if nox_error:
            self.ctx.error("Failed to run the configuration migration tool on the admin configuration " +
                           "we retrieved from device - {}.".format(nox_error))

        conversion_success = False

        match = re.search("Filename[\sA-Za-z\n]*[-\s]*\S*\s+(\d*)\s+\d*\(\s*\d*%\)\s+\d*\(\s*\d*%\)\s+(\d*)",
                          nox_output)

        if match:
            if match.group(1) == match.group(2):
                conversion_success = True

        if filename == ADMIN_CONFIG_IN_CSM:
            supported_log_name = "supported_config_in_admin_configuration"
            unsupported_log_name = "unsupported_config_in_admin_configuration"
        else:
            supported_log_name = "supported_config_in_xr_configuration"
            unsupported_log_name = "unsupported_config_in_xr_configuration"

        if conversion_success:

            if self._all_configs_supported(nox_output):
                self.ctx.info("Configuration {} was migrated successfully. ".format(filename) +
                              "No unsupported configurations found.")
            else:
                self._create_config_logs(os.path.join(fileloc, filename.split(".")[0] + ".csv"),
                                         supported_log_name, unsupported_log_name,
                                         hostname, filename)

                self.ctx.info("Configurations that are unsupported in eXR were removed in {}. ".format(filename) +
                              "Please look into {} and {}.".format(unsupported_log_name, supported_log_name))
        else:
            self._create_config_logs(os.path.join(fileloc, filename.split(".")[0] + ".csv"),
                                     supported_log_name, unsupported_log_name, hostname, filename)

            self.ctx.error("Unknown configurations found. Please look into {} ".format(unsupported_log_name) +
                           "for unprocessed configurations, and {} for known/supported configurations".format(
                           unsupported_log_name, supported_log_name)
                           )

    def _resize_eusb(self):
        """Resize the eUSB partition on device - Run the /pkg/bin/resize_eusb script on device(from ksh)."""
        self.ctx.send("run", wait_for_string="#")
        output = self.ctx.send("ksh /pkg/bin/resize_eusb", wait_for_string="#")
        self.ctx.send("exit")
        if "eUSB partition completed." not in output:
            self.ctx.error("eUSB partition failed. Please check session.log.")
        output = self.ctx.send("show media")

        eusb_size = re.search("/harddiskb:.* ([.\d]+)G", output)

        if not eusb_size:
            self.ctx.error("Unexpected output from CLI 'show media'.")

        if eusb_size.group(1) < "1.0":
            self.ctx.error("/harddiskb:/ is smaller than 1 GB after running /pkg/bin/resize_eusb. " +
                           "Please make sure that the device has either RP2 or RSP4.")

    def _check_fpd(self, iosxr_run_nodes):
        """
        Check the versions of migration related FPD's on device. Return a dictionary
        that tells which FPD's on which node needs upgrade.

        :param iosxr_run_nodes: a list of strings representing all nodes(RSP/RP/LC) on device
                                that we actually will need to make sure that the FPD upgrade
                                later on completes successfully.
        :return: a dictionary with string FPD type as key, and a list of the string names of
                 nodes(RSP/RP/LC) as value.
        """
        fpdtable = self.ctx.send("show hw-module fpd location all")

        subtype_to_locations_need_upgrade = {}

        for fpdtype in FPDS_CHECK_FOR_UPGRADE:
            match_iter = re.finditer(NODE + "[-.A-Z0-9a-z\s]*?" + fpdtype + "[-.A-Z0-9a-z\s]*?(No|Yes)", fpdtable)

            for match in match_iter:
                if match.group(1) in iosxr_run_nodes:
                    if match.group(2) == "No":
                        if fpdtype not in subtype_to_locations_need_upgrade:
                            subtype_to_locations_need_upgrade[fpdtype] = []
                        subtype_to_locations_need_upgrade[fpdtype].append(match.group(1))

        return subtype_to_locations_need_upgrade

    def _ensure_updated_fpd(self, packages, iosxr_run_nodes, version):
        """
        Upgrade FPD's if needed.
        Steps:
        1. Check if the FPD package is already active on device.
           Error out if not.
        2. Check if the same FPD SMU is already active on device.
           (Possibly by a previous Pre-Migrate action)
        3. Install add, activate and commit the FPD SMU if SMU is not installed.
        4. Check version of the migration related FPD's. Get the dictionary
           of FPD types mapped to locations for which we need to check for
           upgrade successs.
        5. Force install the FPD types that need upgrade on all locations.
           Check FPD related sys log to make sure all necessary upgrades
           defined by the dictionary complete successfully.

        :param packages: all user selected packages from scheduling the Pre-Migrate
        :param iosxr_run_nodes: the list of string nodes names we get from running
                                self._get_supported_iosxr_run_nodes(supported_hw)
        :param version: the current version of software. i.e., "5.3.3"
        :return: True if no error occurred.
        """

        self.ctx.info("Checking if FPD package is actively installed...")
        active_packages = self.ctx.send("show install active summary")

        match = re.search("fpd", active_packages)

        if not match:
            self.ctx.error("No FPD package is active on device. Please install the FPD package on device first.")

        if version < RELEASE_VERSION_DOES_NOT_NEED_FPD_SMU:

            self.ctx.info("Checking if the FPD SMU has been installed...")
            pie_packages = []
            for package in packages:
                if package.find(".pie") > -1:
                    pie_packages.append(package)

            if len(pie_packages) != 1:
                self.ctx.error("Please select exactly one FPD SMU pie on server repository for FPD upgrade. " +
                               "The filename must contains '.pie'")

            name_of_fpd_smu = pie_packages[0].split(".pie")[0]

            install_summary = self.ctx.send("show install active summary")

            match = re.search(name_of_fpd_smu, install_summary)

            if match:
                self.ctx.info("The selected FPD SMU {} is found already active on device.".format(pie_packages[0]))
            else:
                self._run_install_action_plugin(InstallAddPlugin, "add")
                self._run_install_action_plugin(InstallActivatePlugin, "activate")
                self._run_install_action_plugin(InstallCommitPlugin, "commit")

        # check for the FPD version, if FPD needs upgrade,
        self.ctx.info("Checking FPD versions...")
        subtype_to_locations_need_upgrade = self._check_fpd(iosxr_run_nodes)

        if subtype_to_locations_need_upgrade:

            # Force upgrade all FPD's in RP and Line card that need upgrade, with the FPD pie or both the FPD
            # pie and FPD SMU depending on release version
            self._upgrade_all_fpds(subtype_to_locations_need_upgrade)

        return True

    def _run_install_action_plugin(self, plugin_class, plugin_action):
        """Instantiate other install actions(install add, activate and commit) on same given packages"""
        self.ctx.info("FPD upgrade - install {} the FPD SMU...".format(plugin_action))
        try:
            install_plugin = plugin_class(self.ctx)
            install_plugin.run()
        except PluginError as e:
            self.ctx.error("Failed to install {} the FPD SMU - ({}): {}".format(plugin_action,
                                                                                e.errno, e.strerror))
        except AttributeError:
            self.ctx.error("Failed to install {} the FPD SMU. Please check session.log for details of failure.".format(
                plugin_action)
            )

    def _upgrade_all_fpds(self, subtype_to_locations_need_upgrade):
        """Force upgrade certain FPD's on all locations. Check for success. """
        def send_newline(ctx):
            ctx.ctrl.sendline()
            return True

        def send_yes(ctx):
            ctx.ctrl.sendline("yes")
            return True

        def error(ctx):
            ctx.message = "Error upgrading FPD."
            return False

        def timeout(ctx):
            ctx.message = "Timeout upgrading FPD."
            return False

        for fpdtype in subtype_to_locations_need_upgrade:

            self.ctx.info("FPD upgrade - start to upgrade FPD {} on all locations".format(fpdtype))

            CONFIRM_CONTINUE = re.compile("Continue\? \[confirm\]")
            CONFIRM_SECOND_TIME = re.compile("Continue \? \[no\]:")
            UPGRADE_END = re.compile("FPD upgrade has ended.")

            PROMPT = self.ctx.prompt
            TIMEOUT = self.ctx.TIMEOUT

            events = [PROMPT, CONFIRM_CONTINUE, CONFIRM_SECOND_TIME, UPGRADE_END, TIMEOUT]
            transitions = [
                (CONFIRM_CONTINUE, [0], 1, send_newline, TIMEOUT_FOR_FPD_UPGRADE),
                (CONFIRM_SECOND_TIME, [1], 2, send_yes, TIMEOUT_FOR_FPD_UPGRADE),
                (UPGRADE_END, [1, 2], 3, None, 120),
                (PROMPT, [3], -1, None, 0),
                (PROMPT, [1, 2], -1, error, 0),
                (TIMEOUT, [0, 1, 2], -1, timeout, 0),

            ]

            if not self.ctx.run_fsm("Upgrade FPD",
                                    "admin upgrade hw-module fpd {} force location all".format(fpdtype),
                                    events, transitions, timeout=30):
                self.ctx.error("Error while upgrading FPD subtype {}. Please check session.log".format(fpdtype))

            fpd_log = self.ctx.send("show log | include fpd")

            for location in subtype_to_locations_need_upgrade[fpdtype]:

                pattern = "Successfully\s*(?:downgrade|upgrade)\s*{}.*location\s*{}".format(fpdtype, location)
                fpd_upgrade_success = re.search(pattern, fpd_log)

                if not fpd_upgrade_success:
                    self.ctx.error("Failed to upgrade FPD subtype {} on location {}. ".format(fpdtype, location) +
                                   "Please check session.log.")
        return True

    def _create_config_logs(self, csvfile, supported_log_name, unsupported_log_name, hostname, filename):
        """
        Create two logs for migrated configs that are unsupported and supported by eXR.
        They are stored in the same directory as session log, for user to view.

        :param csvfile: the string csv filename generated by running NoX on original config.
        :param supported_log_name: the string filename for the supported configs log
        :param unsupported_log_name: the string filename for the unsupported configs log
        :param hostname: string hostname of device, as recorded on CSM.
        :param filename: string filename of original config
        :return: None if no error occurred
        """

        supported_config_log = os.path.join(self.ctx.log_directory, supported_log_name)
        unsupported_config_log = os.path.join(self.ctx.log_directory, unsupported_log_name)
        try:
            with open(supported_config_log, 'w') as supp_log:
                with open(unsupported_config_log, 'w') as unsupp_log:
                    supp_log.write('Configurations Known and Supported to the NoX Conversion Tool \n \n')

                    unsupp_log.write('Configurations Unprocessed by the NoX Conversion Tool (Comments, Markers,' +
                                     ' or Unknown/Unsupported Configurations) \n \n')

                    supp_log.write('{0[0]:<8} {0[1]:^20} \n'.format(("Line No.", "Configuration")))
                    unsupp_log.write('{0[0]:<8} {0[1]:^20} \n'.format(("Line No.", "Configuration")))
                    with open(csvfile, 'rb') as csvfile:
                        reader = csv.reader(csvfile)
                        for row in reader:
                            if len(row) >= 3 and row[1].strip() == "KNOWN_SUPPORTED":
                                supp_log.write('{0[0]:<8} {0[1]:<} \n'.format((row[0], row[2])))
                            elif len(row) >= 3:
                                unsupp_log.write('{0[0]:<8} {0[1]:<} \n'.format((row[0], row[2])))

                    msg = "\n \nPlease find original configuration in csm_data/migration/{}/{} \n".format(hostname,
                                                                                                          filename)
                    supp_log.write(msg)
                    unsupp_log.write(msg)
                    if filename.split('.')[0] == 'admin':
                        msg2 = "The final converted configuration is in csm_data/migration/" + \
                               hostname + "/" + CONVERTED_ADMIN_CAL_CONFIG_IN_CSM + \
                               " and csm_data/migration/" + hostname + "/" + CONVERTED_ADMIN_XR_CONFIG_IN_CSM
                    else:
                        msg2 = "The final converted configuration is in csm_data/migration/" + \
                               hostname + "/" + CONVERTED_XR_CONFIG_IN_CSM
                    supp_log.write(msg2)
                    unsupp_log.write(msg2)
                    csvfile.close()
                unsupp_log.close()
            supp_log.close()
        except:
            self.ctx.error("Error writing diagnostic files - in " + self.ctx.log_directory +
                           " during configuration migration.")

    def _filter_server_repository(self, server):
        """Filter out LOCAL server repositories and only keep TFTP, FTP and SFTP"""
        if not server:
            self.ctx.error("Pre-Migrate missing server repository object.")
        if server.server_type != ServerType.FTP_SERVER and server.server_type != ServerType.TFTP_SERVER and \
           server.server_type != ServerType.SFTP_SERVER:
            self.ctx.error("Pre-Migrate does not support " + server.server_type + " server repository.")

    def _save_config_to_csm_data(self, files, admin=False):
        """
        Copy the admin configuration or IOS-XR configuration
        from device to csm_data.

        :param files: the full local file paths for configs.
        :param admin: True if asking for admin config, False otherwise.
        :return: None
        """

        try:
            cmd = "admin show run" if admin else "show run"
            output = self.ctx.send(cmd, timeout=TIMEOUT_FOR_COPY_CONFIG)
            ind = output.rfind('Building configuration...\n')

        except pexpect.TIMEOUT:
            self.ctx.error("CLI '{}' timed out after 1 hour.".format(cmd))

        for file_path in files:
            # file = '../../csm_data/migration/<hostname>' + filename
            file_to_write = open(file_path, 'w+')
            file_to_write.write(output[(ind+1):])
            file_to_write.close()

    def _handle_configs(self, hostname, server, repo_url, fileloc, nox_to_use, config_filename):
        """
        1. Copy admin and XR configs from device to tftp server repository.
        2. Copy admin and XR configs from server repository to csm_data/migration/<hostname>/
        3. Copy admin and XR configs from server repository to session log directory as
           show-running-config.txt and admin-show-running-config.txt for comparisons
           after Migrate or Post-Migrate. (Diff will be generated.)
        4. Run NoX on admin config first. This run generates 1) eXR admin/calvados config
           and POSSIBLY 2) eXR XR config.
        5. Run NoX on XR config if no custom eXR config has been selected by user when
           Pre-Migrate is scheduled. This run generates eXR XR config.
        6. Copy all converted configs to the server repository and then from there to device.
           Note if user selected custom eXR XR config, that will be uploaded instead of
           the NoX migrated original XR config.

        :param hostname: string hostname of device, as recorded on CSM.
        :param repo_url: the URL of the selected TFTP server repository. i.e., tftp://223.255.254.245/tftpboot
        :param fileloc: the string path ../../csm_data/migration/<hostname>
        :param nox_to_use: the name of the NoX binary executable
        :param config_filename: the user selected string filename of custom eXR XR config.
                                If it's '', nothing was selected.
                                If selected, this file must be in the server repository.
        :return: None if no error occurred.
        """

        self.ctx.info("Saving the current configurations on device into server repository and csm_data")

        self._save_config_to_csm_data([os.path.join(fileloc, ADMIN_CONFIG_IN_CSM),
                                       os.path.join(self.ctx.log_directory,
                                       self.ctx.normalize_filename("admin show running-config"))
                                       ], admin=True)

        self._save_config_to_csm_data([os.path.join(fileloc, XR_CONFIG_IN_CSM),
                                       os.path.join(self.ctx.log_directory,
                                       self.ctx.normalize_filename("show running-config"))
                                       ], admin=False)

        self.ctx.info("Converting admin configuration file with configuration migration tool")
        self._run_migration_on_config(fileloc, ADMIN_CONFIG_IN_CSM, nox_to_use, hostname)

        # ["admin.cal"]
        config_files = [CONVERTED_ADMIN_CAL_CONFIG_IN_CSM]
        # ["admin_calvados.cfg"]
        config_names_on_device = [ADMIN_CAL_CONFIG_ON_DEVICE]
        if not config_filename:

            self.ctx.info("Converting IOS-XR configuration file with configuration migration tool")
            self._run_migration_on_config(fileloc, XR_CONFIG_IN_CSM, nox_to_use, hostname)

            # "xr.iox"
            config_files.append(CONVERTED_XR_CONFIG_IN_CSM)
            # "iosxr.cfg"
            config_names_on_device.append(XR_CONFIG_ON_DEVICE)

        # admin.iox
        if os.path.isfile(os.path.join(fileloc, CONVERTED_ADMIN_XR_CONFIG_IN_CSM)):
            config_files.append(CONVERTED_ADMIN_XR_CONFIG_IN_CSM)
            config_names_on_device.append(ADMIN_XR_CONFIG_ON_DEVICE)

        self.ctx.info("Uploading the migrated configuration files to server repository and device.")

        config_names_in_repo = [hostname + "_" + config_name for config_name in config_files]

        if self._upload_files_to_server_repository([os.path.join(fileloc, config_name)
                                                    for config_name in config_files],
                                                   server, config_names_in_repo):

            if config_filename:
                config_names_in_repo.append(config_filename)
                # iosxr.cfg
                config_names_on_device.append(XR_CONFIG_ON_DEVICE)

            self._copy_files_to_device(server, repo_url, config_names_in_repo,
                                       [IMAGE_LOCATION + config_name
                                        for config_name in config_names_on_device],
                                       timeout=TIMEOUT_FOR_COPY_CONFIG)

    def _get_exr_tar_package(self, packages):
        """Find out which version of eXR we are migrating to from the name of tar file"""
        image_pattern = re.compile("asr9k.*\.tar.*")
        for package in packages:
            if image_pattern.match(package):
                return package
        self.ctx.error("No ASR9K IOS XR 64 Bit tar file found in packages.")

    def _find_nox_to_use(self):
        """
        Find out if the linux system is 32 bit or 64 bit. NoX currently only has a binary executable
        compiled for 64 bit.
        """
        check_32_or_64_system = subprocess.Popen(['uname', '-a'], stdout=subprocess.PIPE)

        out, err = check_32_or_64_system.communicate()

        if err:
            self.ctx.error("Failed to execute 'uname -a' on the linux system.")

        if "x86_64" in out:
            return NOX_64_BINARY
        else:
            self.ctx.error("The configuration migration tool NoX is currently not available for 32 bit linux system.")

    def run(self):
        server_repo_url = None
        try:
            server_repo_url = self.ctx.server_repository_url
        except AttributeError:
            pass

        if server_repo_url is None:
            self.ctx.error("No repository provided.")

        try:
            packages = self.ctx.software_packages
        except AttributeError:
            self.ctx.error("No package list provided")

        try:
            config_filename = self.ctx.pre_migrate_config_filename
        except AttributeError:
            pass

        try:
            server = self.ctx.get_server
        except AttributeError:
            self.ctx.error("No server repository selected")

        if server is None:
            self.ctx.error("No server repository selected")

        try:
            override_hw_req = self.ctx.pre_migrate_override_hw_req
        except AttributeError:
            self.ctx.error("No indication for whether to override hardware requirement or not.")

        with open(SUPPORTED_HW_JSON) as supported_hw_file:
            supported_hw = json.load(supported_hw_file)

        exr_image = self._get_exr_tar_package(packages)
        version_match = re.findall("\d+\.\d+\.\d+", exr_image)
        if version_match:
            exr_version = version_match[0]
        else:
            self.ctx.error("The selected tar file is missing release number in its filename.")

        if supported_hw.get(exr_version) is None:
            self.ctx.error("Missing hardware support information available for release {}.".format(exr_version))

        self._filter_server_repository(server)

        hostname_for_filename = re.sub("[()\s]", "_", self.ctx._csm.host.hostname)
        hostname_for_filename = re.sub("_+", "_", hostname_for_filename)

        fileloc = self.ctx.migration_directory + hostname_for_filename

        if not os.path.exists(fileloc):
            os.makedirs(fileloc)

        self.ctx.info("Checking if some migration requirements are met.")

        if supported_hw.get(exr_version) and not override_hw_req:
            self.ctx.info("Check if all RSP/RP/FAN/PEM on device are supported for migration.")
            self._check_if_rp_fan_pem_supported_and_in_valid_state(supported_hw[exr_version])

        iosxr_run_nodes = self._get_supported_iosxr_run_nodes(supported_hw[exr_version])

        if len(iosxr_run_nodes) == 0:
            self.ctx.error("No RSP/RP or Linecard on the device is supported for migration to ASR9K-X64.")

        if self.ctx.os_type != "XR":
            self.ctx.error('Device is not running ASR9K Classic XR. Migration action aborted.')

        match_version = re.search("(\d\.\d\.\d).*", self.ctx.os_version)

        if not match_version:
            self.ctx.error("Bad os_version.")

        version = match_version.group(1)

        if version < MINIMUM_RELEASE_VERSION_FOR_MIGRATION:
            self.ctx.error("The minimal release version required for migration is 5.3.3. " +
                           "Please upgrade to at lease R5.3.3 before scheduling migration.")
        try:
            node_status_plugin = NodeStatusPlugin(self.ctx)
            node_status_plugin.run()
        except PluginError:
            self.ctx.error("Not all nodes are in valid states. Pre-Migrate aborted. " +
                           "Please check session.log to trouble-shoot.")

        self._ping_repository_check(server_repo_url)

        self.ctx.info("Resizing eUSB partition.")
        self._resize_eusb()

        # nox_to_use = self.ctx.migration_directory + self._find_nox_to_use()

        nox_to_use = self.ctx.migration_directory + NOX_FOR_MAC

        if not os.path.isfile(nox_to_use):
            self.ctx.error("The configuration conversion tool {} is missing. ".format(nox_to_use) +
                           "CSM should have downloaded it from CCO when migration actions were scheduled.")

        self._handle_configs(hostname_for_filename, server,
                             server_repo_url, fileloc, nox_to_use, config_filename)

        self.ctx.info("Copying the ASR9K-X64 image from server repository to device.")
        self._copy_files_to_device(server, server_repo_url, [exr_image],
                                   [IMAGE_LOCATION + exr_image], timeout=TIMEOUT_FOR_COPY_IMAGE)

        self._ensure_updated_fpd(packages, iosxr_run_nodes, version)

        return True
