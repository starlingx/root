#!/usr/bin/python3

#
# Copyright (c) 2021,2025 Wind River Systems, Inc.
#
# SPDX-License-Identifier: Apache-2.0
#

import argparse
import configparser
from datetime import datetime
import json
import os
import pprint
import subprocess
import sys
import urllib.parse
import xml.etree.ElementTree as ET

import requests

SUCCESS = 0
FAILURE = 1
DEFAULT_MANIFEST_FILE='default.xml'

# Cache the repo manifest file information
REPO_MANIFEST = None


def responseCorrection(content):
    return content[5:]


def handleList(dargs):
    pass


def etree_to_dict(elem):
    """ Convert in elementTree to a dictionary
    """
    d = {elem.tag: {} if elem.attrib else None}
    # Handle element's children
    children = list(elem)
    if children:
        dd = {}
        for child in children:
            child_dict = etree_to_dict(child)
            for k, v in child_dict.items():
                if k in dd:
                    if not isinstance(dd[k], list):
                        dd[k] = [dd[k]]
                    dd[k].append(v)
                else:
                    dd[k] = v
        d[elem.tag] = dd
    # Handle element's attributes
    if elem.attrib:
        d[elem.tag].update((k, v) for k, v in elem.attrib.items())
    # Handle element's text
    text = elem.text.strip() if elem.text and elem.text.strip() else None
    if text:
        d[elem.tag]['#text'] = text
    return d

def addGerritQuery(query_string, field_name, target_values):
    """ Add a query for a specific field.
    """
    if not type(target_values) is list:
        target_values = [target_values]
    if len(target_values) == 0:
        return query_string
    elif len(target_values) == 1:
        return '{} {}:"{}"'.format(query_string, field_name, target_values[0])
    else:
        assemble = '{} ({}:"{}"'.format(query_string, field_name, target_values[0])
        for val in target_values[1:]:
            assemble = '{} OR {}:"{}"'.format(assemble, field_name, val)
        assemble = assemble + ')'
        return assemble


def gerritQuery(query):
    url_query = urllib.parse.urljoin('https://' + query['gerrit'], 'changes/')
    if query['verbose'] >= 1:
        print('gerritQuery args: {}'.format(url_query))
    # GET /changes/?q=topic:"my-topic"&o=CURRENT_REVISION&o=DOWNLOAD_COMMANDS HTTP/1.0
    # GET /changes/?q=topic:"my-topic"+status:open&o=CURRENT_REVISION&o=DOWNLOAD_COMMANDS HTTP/1.0
    # GET /changes/?q=topic:"my-topic"+(status:open OR status:merged)&o=CURRENT_REVISION&o=DOWNLOAD_COMMANDS HTTP/1.0
    query_string = addGerritQuery('', 'topic', query['topic'])
    query_string = addGerritQuery(query_string, 'status', query['status'])
    query_string = addGerritQuery(query_string, 'branch', query['branch'])
    if 'repo' in query:
        repo = query['repo']
        if repo.endswith('.git'):
            repo = repo[:-len('.git')]
        query_string = addGerritQuery(query_string, 'repo', repo)
    if query['verbose'] >= 1:
        print('gerritQuery string: {}'.format(query_string))
    params = {'q': query_string,
              'o': ['CURRENT_REVISION', 'DOWNLOAD_COMMANDS']}
    r = requests.get(url=url_query, params=params)
    content = responseCorrection(r.text)
    data = json.loads(content)
    if query['verbose'] >= 5:
        print('gerritQuery results:')
        pprint.pprint(data)
    sorted_data = sorted(data, key=lambda x: x["_number"])
    if query['verbose'] >= 4:
        print('gerritQuery results:')
        pprint.pprint(sorted_data)
    return sorted_data

def truncate_ns_to_us(ts: str) -> datetime:
    if '.' in ts:
        base, frac = ts.split('.')
        frac = (frac + '000000')[:6]  # pad and truncate to 6 digits
        ts = f"{base}.{frac}"
    return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S.%f")

def readRepoManifest(repo_root_dir, manifest_file=None):
    if manifest_file:
        manifest_path = os.path.join(repo_root_dir, '.repo', 'manifests', manifest_file)
    else:
        manifest_path = os.path.join(repo_root_dir, '.repo', 'manifest.xml')
    print('Reading manifest file: {}'.format(manifest_path))
    tree = ET.parse(manifest_path)
    root = tree.getroot()
    manifest = {}
    manifest['root'] = repo_root_dir
    manifest['file_name'] = manifest_file
    manifest['path'] = manifest_path
    manifest['remote'] = {}
    manifest['project'] = {}
    manifest['default'] = {}
    for element in root.findall('remote'):
        remote_name = element.get('name')
        fetch_url = element.get('fetch')
        push_url = element.get('pushurl')
        review = element.get('review')
        revision = element.get('revision')
        manifest['remote'][remote_name]={'fetch': fetch_url, 'push_url': push_url, 'review': review, 'revision': revision}
    for element in root.findall('project'):
        project_name = element.get('name')
        remote_name = element.get('remote')
        path = element.get('path')
        revision = element.get('revision')
        groups = element.get('groups')
        manifest['project'][project_name]={'remote': remote_name, 'path': path, 'revision': revision, 'groups': groups}
    for element in root.findall('default'):
        remote_name = element.get('remote')
        revision = element.get('revision')
        manifest['default']['remote'] = remote_name
        manifest['default']['revision'] = revision
    for element in root.findall('include'):
        include_name = element.get('name')
        include_manifest = readRepoManifest(repo_root_dir, include_name)
        manifest = {**manifest, **include_manifest}
    return manifest

def RepoManifestProjectList(manifest):
    return list(manifest['project'].keys())

def RepoManifestRemoteList(manifest):
    return list(manifest['remote'].keys())

def RepoManifestProjectInfo(manifest, project_name, use_defaults=True, abs_path=True):
    project_info = None
    if project_name not in manifest['project']:
        return None
    project_info = manifest['project'][project_name]
    if use_defaults:
        if project_info['remote'] is None:
            project_info['remote'] = manifest['default']['remote']
        if project_info['revision'] is None:
            if project_info['remote'] is not None:
                project_info['revision'] = manifest['remote'][project_info['remote']]['revision']
        if project_info['revision'] is None:
            project_info['revision'] = manifest['default']['revision']
    if abs_path:
        project_info['path'] = os.path.join(manifest['root'], project_info['path'])
    return project_info

def RepoManifestProjectPath(manifest, project_name, abs_path=True):
    project_info = RepoManifestProjectInfo(manifest, project_name, abs_path=abs_path)
    if project_info is None:
        return None
    return project_info['path']


def extractDownloadCommand(dargs, change):
    rev = change.get('revisions')
    key = list(rev.keys())[0]
    command = rev.get(key)
    command = command.get('fetch')
    command = command.get('anonymous http')
    command = command.get('commands')
    command = command.get(dargs['download_strategy'], None)
    if not command:
        raise Exception("Can't get command for {} download strategy!".format(
            dargs['download_strategy']))
    return command


def checkSkipChange(dargs, change_id, max_search_depth=100):
    """ Determine if the change should be skipped.
    Determine based on the Change-Id: in commit message.
    @param dargs: Parsed dargs
    @param change_id: A gerrit Change-Id to be skipped
    @param max_search_depth: Limit the search depth to a certain number
                             to speed up things.
    @return: True if the change should be skipped
    """
    cmd = ['git', 'rev-list', 'HEAD', '--count', '--no-merges']
    output = subprocess.check_output(
        cmd
        , errors="strict").strip()
    rev_count = int(output)
    if dargs['verbose']>= 6:
        print(rev_count)
    # TODO param for max_search_depth
    for i in range(min(rev_count - 1, max_search_depth)):
        cmd = ['git', 'rev-list', '--format=%B', '--max-count',
               '1', 'HEAD~{}'.format(i)]
        output = subprocess.check_output(
            cmd
            , errors="strict").strip()
        if dargs['verbose']>= 6:
            print(output)
        # TODO avoid false positives, search just last occurrence
        if 'Change-Id: {}'.format(change_id) in output:
            print('Found {} in git log'.format(change_id))
            return True
    return False


def validateHandleRepoArgs(dargs):
    """ Validate dargs for repositories that use Repo tool
    @param dargs: Args from ArgumentParser
    """
    print('Using repo root dir {}'.format(dargs['repo_root_dir']))
    if not os.path.exists(dargs['repo_root_dir']):
        print('{} does not exist'.format(dargs['repo_root_dir']))
        return False
    # print('Using gerrit {}'.format(dargs['gerrit']))
    print('Using download strategy {}'.format(dargs['download_strategy']))
    # print('Using review statuses {}'.format(dargs['status']))
    if dargs['merge_fixer']:
        if os.path.exists(dargs['merge_fixer']):
            print('Using script to attempt automatic merge conflicts '
                  'resolution: {}'.format(dargs['merge_fixer']))
        else:
            print('File {} does not exist'.format(dargs['merge_fixer']))
            return False
    return True


def handleRepo(args):
    """ Main logic for repositories that use repo tool
    @param args: Args from ArgumentParser
    """
    global REPO_MANIFEST
    dargs = vars(args)
    validateHandleRepoArgs(dargs)
    tool_cwd = os.path.dirname(os.path.abspath(__file__))
    REPO_MANIFEST = readRepoManifest(dargs['repo_root_dir'])
    for project in RepoManifestProjectList(REPO_MANIFEST):
        project_info = RepoManifestProjectInfo(REPO_MANIFEST, project)
        project_path = project_info['path']
        print(project_info)
        git_review_info = readGitReview(project_info['path'])
        if git_review_info is None:
            print('Skipping {}: .gitreview missing'.format(project))
            continue
        if git_review_info['branch'] != project_info['revision']:
            print('Skipping {}: branch mismatch between manifest and gitreview'.format(project))
            continue
        query = {}
        query['gerrit'] = git_review_info['host']
        query['repo'] = git_review_info['project']
        query['branch'] = git_review_info['branch']
        query['topic'] = dargs['topic']
        query['status'] = 'open'
        query['verbose'] = dargs['verbose']
        if dargs['verbose']>= 1:
            print(project, ' ', query)
        query_results = gerritQuery(query)
        print('Found {} matching reviews in {}'.format(len(query_results),project))
        for query_result in query_results:
            # Get project of the change
            project = query_result.get('project')
            project_name, repository_name = project.split('/')
            change_id = query_result.get('change_id')
            print('Detected change number {} ID {} project {} repository {}'
                  ''.format(query_result.get('_number', ''),
                            change_id,
                            project_name,
                            repository_name))
            download_command = extractDownloadCommand(dargs, query_result)
            os.chdir(project_path)
            print("Changed working directory to: {}".format(os.getcwd()))
            # Check if the change should be skipped
            if dargs['avoid_re_download'] and checkSkipChange(dargs, change_id):
                print('Skipping {}'.format(change_id))
                continue
            # Apply commit
            cmds = download_command.split('&&')
            print('Commands to be executed {}'.format(cmds))
            try:
                oldenv = os.environ.copy()
                env={'GIT_RERERE_AUTOUPDATE': '0'}
                env = { **oldenv, **env }
                for cmd in list(cmds):
                    cmd = cmd.strip('"')
                    print('Command to be executed {}'.format(cmd))
                    if not dargs['dry_run']:
                        output = subprocess.check_output(
                            cmd
                            , env=env
                            , errors="strict", shell=True).strip()
                        print('Executed: \n{}'.format(output))
            except Exception as e:
                pprint.pprint(e)
                if dargs['merge_fixer'] and not dargs['dry_run']:
                    print('Using merge fixer!')
                    rc = runMergeFixer(dargs, project_path, tool_cwd)
                    return rc
                else:
                    print('Check for unresolved merge conflict')
                    return False
    return True


def runMergeFixer(dargs, project_path, tool_cwd):
    # Run fixer
    fixer = '{}'.format(os.path.join(tool_cwd, dargs['merge_fixer']))
    cmd = [fixer]
    fixer_rc, _ = run_cmd(cmd, shell=False, halt_on_exception=False)
    # Abort in case of fixer run failure
    if fixer_rc == FAILURE:
        print('Fixer failed, aborting!!!')
        return False


def run_cmd(cmd, shell=False, halt_on_exception=False):
    # TODO improve logging, but not worth at the moment.
    # LIMITATION
    # Now we could automate up to doing the cherry-pick continue.
    # `git cherry-pick --continue` opens a text editor and freezes terminal
    # Need to figure a way to go around that.
    try:
        print('Running {}:\n'.format(cmd))
        p1 = subprocess.Popen(
            cmd,
            errors="strict",
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True)
        output, error = p1.communicate()
        print('{}\n'.format(output))
        found_exception = False
        if p1.returncode != 0:
            print('stderr: {}\n'.format(error))
            found_exception = True
    except Exception as e:
        found_exception = True
        pprint.pprint(e)
    finally:
        if found_exception and halt_on_exception:
            exit(1)
        if not found_exception:
            return SUCCESS, output
    return FAILURE, output


def readGitReview(gitRoot):
    result = {}
    config = configparser.ConfigParser()
    git_review_path = os.path.join(gitRoot, '.gitreview')
    if not os.path.exists(git_review_path):
        return None
    config.read(os.path.join(gitRoot, '.gitreview'))
    result['host'] = config['gerrit']['host']
    result['port'] = int(config['gerrit']['port'])
    result['project'] = config['gerrit']['project']
    result['branch'] = config['gerrit'].get('defaultbranch', 'master')
    return result

def main():
    parser = argparse.ArgumentParser(description='Tool to sync a Gerrit topic(s)',
                                     formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                                     epilog='''Use %(prog)s subcommand --help to get help for all of parameters''')

    parser.add_argument('--verbose', '-v', action='count', default=0, help='Verbosity level')

    subparsers = parser.add_subparsers(title='Repository type control Commands',
                                       help='...')

    # TODO GIT
    repo_parser = subparsers.add_parser('git',
                                        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                                        help='Command for handling a git managed project... not supported yet')
    repo_parser.set_defaults(handle=handleList)

    # REPO
    repo_parser = subparsers.add_parser('repo',
                                        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
                                        help='Command for handling a repo managed project')
    repo_parser.add_argument('--topic', '-t',
                             action='append',
                             help='Gerrit topic... can be specified more than once',
                             required=True)
    repo_parser.add_argument('--repo-root-dir', '-rr',
                             help='Path to repo root dir',
                             default=os.getenv('MY_REPO_ROOT_DIR', os.getcwd()),
                             required=False)
    repo_parser.add_argument('--manifest', '-m',
                             help='File name of the manifest file (not path). Otherwise use the manifest selected by the last "repo init"',
                             default=None,
                             required=False)
    repo_parser.add_argument('--download-strategy', '-ds',
                             help='Strategy to download the patch: Pull, Cherry Pick, Branch, Checkout',
                             choices=['Pull', 'Cherry Pick', 'Branch', 'Checkout'],
                             default='Cherry Pick',
                             required=False)
    repo_parser.add_argument('--status', '-s',
                             action='append',
                             help='Status of the review... can be specified more than once',
                             choices=['open', 'merged', 'abandoned'],
                             default=['open'],
                             required=False)
    repo_parser.add_argument('--merge-fixer', '-mf',
                             help='Script to be run to attempt auto merge fixing, e.g. pick_both_merge_fixer.py',
                             required=False)
    repo_parser.add_argument('--avoid-re-download', '-ard',
                             action='store_true',
                             help='Avoid re-downloading a commit if it already exists in the git repo.',
                             default=False,
                             required=False)
    repo_parser.add_argument('--dry-run',
                             action='store_true',
                             help='''Simulate, but don't sync''',
                             default=False,
                             required=False)

    repo_parser.set_defaults(handle=handleRepo)

    args = parser.parse_args()

    if hasattr(args, 'handle'):
        rc = args.handle(args)
        if not rc:
            return 1
    else:
        parser.print_help()
        return 1


if __name__ == '__main__':
    exit_code = main()
    sys.exit(exit_code)
