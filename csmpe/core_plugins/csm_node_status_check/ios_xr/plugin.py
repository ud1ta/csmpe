# =============================================================================
# asr9k
#
# Copyright (c)  2016, Cisco Systems
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


import re

from csmpe.plugins import CSMPlugin


class Plugin(CSMPlugin):
    """This plugin checks the states of all nodes"""
    name = "Node Status Check Plugin"
    platforms = {'ASR9K', 'CRS'}
    phases = {'Pre-Upgrade', 'Post-Upgrade'}
    os = {'XR'}

    def _parse_show_platform(self, output):
        inventory = {}
        lines = output.split('\n')
        for line in lines:
            line = line.strip()
            if len(line) > 0 and line[0].isdigit():
                states = re.split('\s\s+', line)
                if not re.search('CPU\d+$', states[0]):
                    continue
                if self.ctx.family == 'ASR9K':
                    node, node_type, state, config_state = states
                elif self.ctx.family == 'CRS':
                    node, node_type, plim, state, config_state = states
                else:
                    self.ctx.warning("Unsupported platform {}".format(self.ctx.family))
                    return None
                entry = {
                    'type': node_type,
                    'state': state,
                    'config_state': config_state
                }
                inventory[node] = entry
        return inventory

    def run(self):
        """
        Platform: ASR9K
        RP/0/RSP0/CPU0:R3#admin show platform
        Tue May 17 08:23:19.612 UTC
        Node            Type                      State            Config State
        -----------------------------------------------------------------------------
        0/RSP0/CPU0     A9K-RSP440-SE(Active)     IOS XR RUN       PWR,NSHUT,MON
        0/FT0/SP        ASR-9006-FAN              READY
        0/1/CPU0        A9K-40GE-E                IOS XR RUN       PWR,NSHUT,MON
        0/2/CPU0        A9K-MOD80-SE              UNPOWERED        NPWR,NSHUT,MON
        0/3/CPU0        A9K-8T-L                  UNPOWERED        NPWR,NSHUT,MON
        0/PM0/0/SP      A9K-3KW-AC                READY            PWR,NSHUT,MON
        0/PM0/1/SP      A9K-3KW-AC                READY            PWR,NSHUT,MON

        Platform: CRS
        RP/0/RP0/CPU0:CRS-X-Deploy2#admin show platform
        Tue May 17 21:11:56.915 UTC
        Node          Type              PLIM               State           Config State
        ------------- ----------------- ------------------ --------------- ---------------
        0/0/CPU0      MSC-X             40-10GbE           IOS XR RUN      PWR,NSHUT,MON
        0/2/CPU0      FP-X              4-100GbE           IOS XR RUN      PWR,NSHUT,MON
        0/3/CPU0      MSC-140G          N/A                UNPOWERED       NPWR,NSHUT,MON
        0/4/CPU0      FP-X              N/A                UNPOWERED       NPWR,NSHUT,MON
        0/7/CPU0      MSC-X             N/A                UNPOWERED       NPWR,NSHUT,MON
        0/8/CPU0      MSC-140G          N/A                UNPOWERED       NPWR,NSHUT,MON
        0/14/CPU0     MSC-X             4-100GbE           IOS XR RUN      PWR,NSHUT,MON
        """
        output = self.ctx.send("admin show platform")
        inventory = self._parse_show_platform(output)
        valid_state = [
            'IOS XR RUN',
            'PRESENT',
            'READY',
            'FAILED',
            'OK',
            'DISABLED',
            'UNPOWERED',
            'ADMIN DOWN',
            'NOT ALLOW ONLIN',  # This is not spelling error
        ]
        for key, value in inventory.items():
            if 'CPU' in key:
                if value['state'] not in valid_state:
                    self.ctx.warning("{}={}: {}".format(key, value, "Not in valid state for upgrade"))
                    break
        else:
            self.ctx.save_data("inventory", inventory)
            self.ctx.info("All nodes in valid state for upgrade")
            return True

        self.ctx.error("Not all nodes in correct state. Upgrade can not proceed")
