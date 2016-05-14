from subprocess import Popen, PIPE, call
from shutil import copyfile
import random
import uuid
import sys
import re
import os

execution_dir = os.path.normpath(os.path.dirname(sys.argv[0]))
help_txt = os.path.join(execution_dir, 'help.txt')


def extra_strip(the_string, extra=0):
    """
    Remove unwanted chars from shell commands output
    :param the_string: string
    :param extra: extra chars/strings to remove
    :return: string
    """
    replace_chars = ['+', '-', '|', ' ', 'b\'', '\\r', '\\n', '\'', ' : ']
    if extra:
        replace_chars.extend(extra)
    for char in replace_chars:
        the_string = the_string.replace(char, '')
    return the_string


def print_help():
    """
    print help text
    :return: none
    """
    with open(help_txt, 'r') as infile:
        for line in infile:
            print(line)


def arg_parse():
    """
    Parse arguments and check for any errors.
    :return: arguments as dictionary {arg_name: arg_value ... }
    """
    # mandatory arguments
    expected_arguments = ['domain-name']
    # optional arguments
    optional_arguments = ['help']

    if '--help' in sys.argv[1:] or len(sys.argv) == 1:
        print_help()
        sys.exit()

    try:
        arguments = {arg.split(':')[0].replace('--', ''): arg.split(':', maxsplit=1)[1]     # split and remove symbols
                     for arg in sys.argv[1:]                                                # iterate over system arguments
                     if arg.split(':', maxsplit=1)[1] is not '' and                         # check if arg is valid
                     arg.split(':')[0].replace('--', '') in expected_arguments + optional_arguments}
    except IndexError:
        print('Bad input, check syntax')
        sys.exit()

    # ensure all mandatory arguments are present
    if any(arg not in arguments for arg in expected_arguments):
        print('Missing some required arguments, please consult usage guide.')
        print_help()
        sys.exit()

    return arguments


def random_mac():
    mac = [ 0x00, 0x16, 0x3e,
        random.randint(0x00, 0x7f),
        random.randint(0x00, 0xff),
        random.randint(0x00, 0xff) ]
    return ':'.join(map(lambda x: "%02x" % x, mac))


def nodedev_parse(output):
    """
    Parse nodedev-list command output to enumerate NICs and ports.
    :param output: command output
    :return: dictionary
    """
    n = 0
    nics = {}
    sorted_nics = {}
    # parse command output to create a basic dictionary
    for index, line in enumerate(output):
        try:
            if re.match('(.*)(pci)(.*)', output[index]) and re.match('(.*)(net)(.*)([0-9a-fA-F]{2}[_]){5}([0-9a-fA-F]{2})(.*)', output[index+2]):
                nics[extra_strip(output[index])] = extra_strip(output[index+2])
        except IndexError:
            pass
    # group/sort ports that are on the same NIC and name them accordingly
    for nic in list(nics.keys()):
        p = 0
        # join all similar ports
        ports = [''.join(x) for x in re.findall("(%s)(_)([0-9])" % nic[0:-2], ' '.join(list(nics.keys())))]
        if len(ports) > 1:
            for port in sorted(ports):

                # skip port if already added
                if any(port in sorted_nics[sorted_port] for sorted_port in sorted_nics):
                    continue

                port_details = nics[port].replace('net_', '').split('_')
                pci_name = port.split('_')
                try:
                    sorted_nics[nic[0:-2]].update({port: {'current_name': '_'.join(port_details[0:len(port_details) - 6]),
                                                          'new_name': 'eth%d' % p,
                                                          'mac': ':'.join(port_details[-6:]),
                                                          'domain': pci_name[1],
                                                          'bus': pci_name[2],
                                                          'slot': pci_name[3],
                                                          'function': pci_name[4]
                                                          }})
                except KeyError:
                    sorted_nics[nic[0:-2]] = {port: {'current_name': '_'.join(port_details[0:len(port_details) - 6]),
                                                     'new_name': 'eth%d' % p,
                                                     'mac': ':'.join(port_details[-6:]),
                                                     'domain': pci_name[1],
                                                     'bus': pci_name[2],
                                                     'slot': pci_name[3],
                                                     'function': pci_name[4]
                                                     }}
                p += 1
    # sort NICs
    for nic in sorted(sorted_nics):
        if len(sorted_nics[nic]) > 1:
            sorted_nics[n] = sorted_nics.pop(nic)
            for port in sorted_nics[n]:
                sorted_nics[n][port]['new_name'] = 'nic{0}_{1}'.format(n, sorted_nics[n][port]['new_name'])
            n += 1
    return sorted_nics


def nic_rename(nics):
    template = 'ACTION=="add", SUBSYSTEM=="net", DRIVERS=="?*", ATTR{{address}}=="{0}", ATTR{{type}}=="1", KERNEL=="eth*", NAME="{1}"\n'

    rules_file = open(r'/etc/udev/rules.d/70-persistent-ipoib.rules', 'a')
    for nic in nics:
        rules_file.write('\n')
        for port in nics[nic]:
            # format commands
            ip_renamer = {0: '/usr/sbin/ip link set {} down'.format(nics[nic][port]['current_name']),
                          1: '/usr/sbin/ip link set {} name {}'.format(nics[nic][port]['current_name'], nics[nic][port]['new_name']),
                          2: '/usr/sbin/ip link set {} up'.format(nics[nic][port]['new_name'])}
            # write to file
            rules_file.write(template.format(nics[nic][port]['mac'], nics[nic][port]['new_name']))
            # run commands
            for command in ip_renamer:
                Popen(ip_renamer[command].split(' ')).wait()
            # delete any configuration files related to renamed nics
                Popen(['/usr/bin/rm', '-rf', '/etc/sysconfig/network-scripts/ifcfg-{}'.format(nics[nic][port]['current_name'])])

    rules_file.close()


def nic_xml_creator(nics):
    base_path = r'/srv/virtual_machines/nics'
    template = '<hostdev mode=\'subsystem\' type=\'pci\' managed=\'yes\'>\n' \
               '    <source>\n' \
               '        <address domain=\'0x{0}\' bus=\'0x{1}\' slot=\'0x{2}\' function=\'0x{3}\'/>\n' \
               '    </source>\n' \
               '</hostdev>'
    # check if directory exists
    if not os.path.exists(base_path):
        os.makedirs(base_path)

    for nic in nics:
        for port in nics[nic]:
            with open(os.path.join(base_path, nics[nic][port]['new_name'] + '.xml'), 'w') as outfile:
                outfile.write(template.format(nics[nic][port]['domain'],
                                              nics[nic][port]['bus'],
                                              nics[nic][port]['slot'],
                                              nics[nic][port]['function']
                                              ))
            Popen(['/usr/bin/virsh', 'nodedev-dettach', port])


def xml_parse(xml_path, new_domain_name):

    master_config = open(xml_path, 'r').readlines()

    for index, line in enumerate(master_config):
        if re.match('(.*)(<name>)(.*)(</name>)(.*)', line):
            master_config[index] = '<name>{}</name>\n'.format(new_domain_name)
        elif re.match('(.*)(<uuid>)(.*)(</uuid>)(.*)', line):
            master_config[index] = '<uuid>{}</uuid>\n'.format(uuid.uuid4())
        elif re.match('(.*)(<source file=\'/srv/virtual_machines/)(.*)(.qcow2\'/>)(.*)', line):
            master_config[index] = '<source file=\'/srv/virtual_machines/{}.qcow2\'/>\n'.format(new_domain_name)
        elif re.match('(.*)(<mac address=)(.*)(/>)(.*)', line):
            master_config[index] = '<mac address=\'{}\'/>\n'.format(random_mac())

    with open(r'/tmp/{}.xml'.format(new_domain_name), 'w') as outfile:
        outfile.writelines(master_config)

    return '/tmp/{}.xml'.format(new_domain_name)


def main():
    arguments = arg_parse()

    proc = Popen(['/usr/bin/virsh', 'nodedev-list', '--tree'], stdout=PIPE)
    out = str(proc.communicate()[0]).split('\\n')
    nics = nodedev_parse(out)

    nic_rename(nics)

    call(['/usr/bin/systemctl', 'disable', 'NetworkManager'])
    call(['/usr/bin/systemctl', 'stop', 'NetworkManager'])

    nic_xml_creator(nics)

    outfile = open('/tmp/master.xml', 'w')
    call(['/usr/bin/virsh', 'dumpxml', arguments['domain-name']], stdout=outfile)
    outfile.close()

    base_path = '/srv/virtual_machines/nics/'
    nics_xml = sorted(file for file in os.listdir(base_path) if os.path.isfile(os.path.join(base_path, file)))

    for bump in range(1, sum(len(nics[nic]) for nic in nics) + 1):
        new_domain_name = '{0}{1:02d}'.format(arguments['domain-name'][0:-2], bump)

        new_xml = xml_parse('/tmp/master.xml', new_domain_name)
        copyfile('/srv/virtual_machines/{}.qcow2'.format(arguments['domain-name']), '/srv/virtual_machines/{}.qcow2'.format(new_domain_name))
        call(['/usr/bin/virsh', 'create', new_xml])
        call(['/usr/bin/virsh', 'attach-device', new_domain_name, '/srv/virtual_machines/nics/{}'.format(nics_xml[bump-1])])




if __name__ == '__main__':
    main()

