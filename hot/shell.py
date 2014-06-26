""" hot is the command-line tool for testing Heat Templates """
import fabric
import json
import os
import re
import signal
import string
import sys
import yaml

from argh import arg, alias, ArghParser

from heatclient.v1 import Client as heatClient
from time import sleep, time
from urlparse import urlparse

# The following lines are needed since we use execfile() to load the test
# script.
from envassert import cron, detect, file, group, package, port, process, service, user
from fabric.api import env, task

import hot.utils

ENV_VARS = ['OS_PASSWORD', 'OS_USERNAME', 'OS_TENANT_ID', 'OS_AUTH_URL',
            'HEAT_URL']

def verify_environment_vars(variables):
    for variable in variables:
        try:
            if os.environ[variable]:
                pass
        except KeyError as exc:
            env_list = ', '.join([str(env) for env in ENV_VARS])
            sys.exit("KeyError: %s not set.\n  Tool requires the following env"
                     "ironmental variables to be set %s" % (exc, env_list))


@alias('test')
@arg('--template', default='.catalog')
@arg('--tests-file', default='tests.yaml')
def do_template_test(args):
    """ Test a template by going through the test scenarios in 'tests.yaml' or
    the tests file specified by the user
    """
    verified_template_directory = hot.utils.repo.check(args)
    template_attr = getattr(args, 'template')
    tests_attr = getattr(args, 'tests_file')
    path_to_template = os.path.join(verified_template_directory, template_attr)
    path_to_tests = os.path.join(verified_template_directory, tests_attr)
    try:
        raw_template = get_raw_yaml_file(args, file_path=path_to_template)
        validated_template = hot.utils.yaml.load(raw_template)
        raw_tests = get_raw_yaml_file(args, file_path=path_to_tests)
        validated_tests = hot.utils.yaml.load(raw_tests)
        verify_environment_vars(ENV_VARS)

    except StandardError as exc:
        sys.exit(exc)

    auth_token = hot.utils.token.get_token(os.environ['OS_AUTH_URL'],
                                           os.environ['OS_USERNAME'],
                                           password=os.environ['OS_PASSWORD'])

    hc = heatClient(endpoint=os.environ['HEAT_URL'], token=auth_token)

    for test in validated_tests['test-cases']:
        stack = launch_test_deployment(hc, validated_template, test)
        if test['resource_tests']:
            try:
                run_resource_tests(hc, stack['stack']['id'],
                                   test)
            except:
                e = sys.exc_info()[0]
                delete_test_deployment(hc, stack)
                sys.exit("Test Failed! Error: %s" % e)

        delete_test_deployment(hc, stack)


def run_resource_tests(hc, stack_id, resource_tests):
    stack_info = hc.stacks.get(stack_id)
    outputs = stack_info.to_dict().get('outputs', [])
    # Sub out {get_output: value} lines
    resource_tests = update_dict(resource_tests['resource_tests'], outputs)

    # For debugging purposes
    # print resource_tests

    if resource_tests['ssh_key_file']:
        ssh_key_file = resource_tests['ssh_key_file']
    else:
        ssh_key_file = 'tmp/private_key'

    # If the file path specified does not exist, create it.
    if not os.path.exists(os.path.dirname(ssh_key_file)):
        os.makedirs(os.path.dirname(ssh_key_file))

    if resource_tests['ssh_private_key']:
        with os.fdopen(os.open(ssh_key_file, os.O_WRONLY | os.O_CREAT, 0600),
                       'w') as handle:
            handle.write(resource_tests['ssh_private_key'])

    for test in resource_tests['tests']:
        test_name = test.keys()[0]
        if test[test_name]['envassert']:
            run_envassert_tasks(test_name, test[test_name])

    delete_file(ssh_key_file, "Deleting ssh_private_key file '%s'." % ssh_key_file)


def update_dict(items, outputs):
    """Update dict based on user input.  This will take anything things like
       { get_output: server_ip } and replace it with the value of the given
       output.
    """
    try:
        for k, v in items.items():
            if isinstance(v, dict):
                items[k] = update_dict(v, outputs)
            elif isinstance(v, list):
                new_list = []
                for e in v:
                    new_list.append(update_dict(e, outputs))
                items[k] = new_list
            elif isinstance(v, int):
                items[k] = v
            elif k == 'get_output':
                return get_output(v, outputs)
            else:
                items[k] = v
    except:
        pass
    return items



def run_envassert_tasks(test_name, test):
    """Setup fabric environment and run envassert script"""
    env_setup = test['envassert']
    if env_setup['env']:
        print "  Preparing environtment to run envassert tests:"
        for k, v in env_setup['env'].iteritems():
            print "    Setting env['%s'] to %s" % (k, v)
            env[k] = v
        execfile(env_setup['env']['fabfile'])
        for task in env_setup['env']['tasks']:
            print "  Launching envassert test '%s', task '%s' on: %s" % (test_name, task, env.hosts)
            fabric.tasks.execute(locals()[task])


def convert_to_array(value):
    """Converts string to array, if `value` is an array, returns `value`"""
    if isinstance(value, list):
        return value
    elif isinstance(value, basestring):
        return [value]


def get_output(key, outputs):
    for output in outputs:
        if output['output_key'] == key:
            return output['output_value']


def delete_test_deployment(hc, stack):
    print "  Deleting %s" % stack['stack']['id']
    hc.stacks.delete(stack['stack']['id'])


def delete_file(file, message="Deleting file '%s'" % file):
    print "  %s" % message
    os.remove(file)


def launch_test_deployment(hc, template, test):
    pattern = re.compile('[\W]')
    stack_name = pattern.sub('_', "%s-%s" % (test['name'], time()))
    data = { "stack_name": stack_name, "template": yaml.safe_dump(template) }

    timeout = get_create_value(test, 'timeout')
    parameters = get_create_value(test, 'parameters')
    retries = get_create_value(test, 'retries') # TODO: Implement retries

    if timeout:
        timeout_value = timeout * 60
        signal.signal(signal.SIGALRM, hot.utils.timeout.handler)
        signal.alarm(timeout_value)
    if parameters:
        data.update({"parameters": parameters})

    print "Launching: %s" % stack_name
    stack = hc.stacks.create(**data)

    if timeout_value:
        print "  Timeout set to %s seconds." % timeout_value

    try:
        monitor_stack(hc, stack)
        signal.alarm(0) # Disable alarm on successful build
    except Exception:
        delete_test_deployment(hc, stack)
        sys.exit("Stack failed to deploy")
    return stack


def get_create_value(test, key):
    try:
        if test['create'][key]:
            return test['create'][key]
    except KeyError:
        return None
    return None


def monitor_stack(hc, stack, sleeper=10):
    incomplete = True
    while incomplete:
        print "  Stack %s in progress, checking again in %s seconds.." % (stack['stack']['id'], sleeper)
        sleep(sleeper)
        status = hc.stacks.get(stack['stack']['id'])
        if status.stack_status == u'CREATE_COMPLETE':
            incomplete = False
            print "  Stack %s built successfully!" % stack['stack']['id']
        elif status.stack_status == u'FAILED':
            print "  Stack %s build failed! Reason:\n  %s" % (stack['stack']['id'], status.stack_status_reason)
            raise Exception("Stack build %s failed" % stack['stack']['id'])


def get_raw_yaml_file(args, file_path=None):
    """

    Reads the contents of any YAML file in the repository as a string

    :param args: the pawn call argument
    :param file_path: the file name with optional additional path
        (subdirectory) or as a URL
    :returns: the string contents of the file

    """
    # file can be a URL or a local file
    file_contents = None
    parsed_file_url = urlparse(file_path)
    if parsed_file_url.scheme == '':
        # Local file
        try:
            _file = open(os.path.expanduser(file_path))
            file_contents = _file.read()
            _file.close()
        except IOError as ioerror:
            raise IOError('Error reading %s. [%s]' % (file_path, ioerror))
    else:
        raise Exception('URL scheme %s is not supported.' %
                        parsed_file_url.scheme)

    return file_contents


def main():
    """Shell entry point for execution"""
    try:
        argparser = ArghParser()
        argparser.add_commands([
            do_template_test,
        ])

        argparser.dispatch()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()
