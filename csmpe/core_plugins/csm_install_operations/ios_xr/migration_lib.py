import time
import re
import json

SUPPORTED_HW_JSON = "migration_supported_hw.json"

NODE = "(\d+/(?:RS?P)?\d+)"


def parse_exr_admin_show_platform(output):
    """Get all RSP/RP/LC string node names matched with the card type."""
    inventory = {}
    lines = output.split('\n')

    for line in lines:
        line = line.strip()
        if len(line) > 0 and line[0].isdigit():
            print "line = *{}*".format(line)
            node = line[:10].strip()
            print "node = *{}*".format(node)
            node_type = line[10:34].strip(),
            print "node_type = *{}*".format(node_type)
            inventory[node] = node_type
    return inventory


def get_all_supported_nodes(ctx, supported_cards):
    """Get the list of string node names(all available RSP/RP/LC) that are supported for migration."""
    supported_nodes = []
    ctx.send("admin")
    output = ctx.send("show platform")
    inventory = parse_exr_admin_show_platform(output)

    node_pattern = re.compile(NODE)
    for node, node_type in inventory.items():
        if node_pattern.match(node):
            for card in supported_cards:
                if card in node_type:
                    supported_nodes.append(node)
                    break
    ctx.send("exit")
    return supported_nodes


def get_version(ctx):
    output = ctx.send("show version | include Version")
    version = re.search("Version\s*?(\d+\.\d+\.\d+)(?:\.\d+I)?", output)
    if not version:
        ctx.error("Failure to retrieve release number.")
    return version.group(1)


def wait_for_final_band(ctx):
    """This is for ASR9K eXR. Wait for all present nodes to come to FINAL Band."""
    exr_version = get_version(ctx)
    with open(SUPPORTED_HW_JSON) as supported_hw_file:
        supported_hw = json.load(supported_hw_file)
    if supported_hw.get(exr_version) is None:
        ctx.error("No hardware support information available for release {}.".format(exr_version))

    supported_nodes = get_all_supported_nodes(ctx, supported_hw.get(exr_version))
    # Wait for all nodes to Final Band
    timeout = 1080
    poll_time = 20
    time_waited = 0

    cmd = "show platform vm"
    while 1:
        # Wait till all nodes are in FINAL Band
        time_waited += poll_time
        if time_waited >= timeout:
            break
        time.sleep(poll_time)
        output = ctx.send(cmd)
        all_nodes_present = True
        for node in supported_nodes:
            if node not in output:
                all_nodes_present = False
                break
        if all_nodes_present and check_sw_status(output):
            return True

    # Some nodes did not come to FINAL Band
    return False


def check_sw_status(output):
    """Check is a node has FINAL Band status"""
    lines = output.splitlines()

    for line in lines:
        line = line.strip()
        if len(line) > 0 and line[0].isdigit():
            sw_status = line[48:64].strip()
            if "FINAL Band" not in sw_status:
                return False
    return True
