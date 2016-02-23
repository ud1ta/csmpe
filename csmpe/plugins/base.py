# =============================================================================
#
# Copyright (c) 2016, Cisco Systems
# All rights reserved.
#
# # Author: Klaudiusz Staniek
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

import abc

import six


@six.add_metaclass(abc.ABCMeta)
class CSMPlugin(object):
    """This is a base class for all plugins. Inheriting from this class is not mandatory,
    however the Plugin class must implement the `run` method.
    The the object constructor must accept single parameter which represents
    the :class:`csmpe.InstallContext` object

    :param ctx: :class:`csmpe.InstallContext` object

    The Plugin class must also have the folowing attributes.
    """
    #: The string representing the name of the plugin.
    name = "Plugin Template"

    #: The set of string representing the phases when the plugin is being executed.
    #: Empty set means that plugin will never be executed. The currently supported values are:
    #: 'Pre-Upgrade', 'Pre-Add', 'Add', 'Pre-Activate', 'Pre-Deactivate', 'Deactivate',
    #: 'Remove', 'Commit'
    phases = {()}

    #: The set of strings representing the supported platforms. Empty set means ANY platform.
    #: The currently supported values are: 'ASR9K', 'CRS', 'NCS6K'
    platforms = {()}

    #: The set of os type strings. The supported values are: 'IOS', 'XR', 'eXR', 'XE'.
    #: Empty set means plugin will be executed regardless of detected os type).
    os = {()}

    def __init__(self, ctx):
        """ This is a constructor of a plugin object. The constructor can be overridden by the plugin code.
        The CSM Plugin Engine passes the :class:`csmpe.InstallContext` object
        as an argument. This context object provides the API interface for the plugin including:

        - Device communication (using condoor)
        - CSM status and information update
        - Progress, error and status logging.

        :param ctx: The install context object :class:`csmpe.InstallContext`
        :return: None
        """
        self.ctx = ctx

    @abc.abstractmethod
    def run(self):
        """
        Method is invoked when the plugin is dispatched by Plugin Engine.
        Must be implemented by the plugin code.

        :param: None
        :return: None
        """
