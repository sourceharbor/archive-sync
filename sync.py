#!/usr/bin/env python3
"""Preserve explicitly listed upstream Git branches and tags without executing code."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed


class SyncError(Exception):
    pass


def git(args, env, deadline, cwd=None):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise SyncError('repository time limit exceeded')
    try:
        result = subprocess.run(
            ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'credential.helper=', *args],
            cwd=cwd, env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=remaining,
        )
    except subprocess.TimeoutExpired:
        raise SyncError('repository time limit exceeded') from None
    if result.returncode:
        # Remote output can contain arbitrary text; never put it in logs or status files.
        raise SyncError(f'git {args[0]} failed (exit {result.returncode})')
    return result.stdout


def isolated_env(home):
    return {
        'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(home),
        'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0',
        'GIT_ASKPASS': '/usr/bin/false', 'SSH_ASKPASS': '/usr/bin/false',
    }


def refs(url, env, deadline):
    output = git(['ls-remote', '--refs', '--heads', '--tags', url], env, deadline)
    found = {}
    for line in output.splitlines():
        sha, ref = line.split('\t', 1)
        if not re.fullmatch(r'[0-9a-f]{40,64}', sha):
            raise SyncError('invalid remote object ID')
        if not ref.startswith(('refs/heads/', 'refs/tags/')):
            raise SyncError('unexpected remote reference')
        found[ref] = sha
    return found


def sync_repository(source, destination, push_url=None, private_key=None,
                    known_hosts=None, timeout=600):
    """URLs may be local paths in tests. main() fixes production hosts and owners."""
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix='sourceharbor-sync-') as tmp:
        work = Path(tmp)
        env = isolated_env(work)
        upstream = refs(source, env, deadline)
        saved = refs(destination, env, deadline)
        if not upstream:
            raise SyncError('upstream has no branches or tags; destination preserved')
        changed = {ref: sha for ref, sha in upstream.items() if saved.get(ref) != sha}
        if not changed:
            return {'state': 'unchanged', 'updated_refs': 0, 'upstream_refs': len(upstream)}
        if any(ref.startswith('refs/tags/') and ref in saved for ref in changed):
            raise SyncError('upstream moved an existing tag; destination preserved')

        repo = work / 'objects.git'
        git(['init', '--bare', str(repo)], env, deadline)
        source_specs = [f'{ref}:refs/source/{ref[5:]}' for ref in changed]
        git(['fetch', '--no-tags', source, *source_specs], env, deadline, repo)
        for ref, expected in changed.items():
            actual = git(['rev-parse', f'refs/source/{ref[5:]}'], env, deadline, repo).strip()
            if actual != expected:
                raise SyncError('upstream changed during fetch; retry on next run')
        existing_heads = [ref for ref in changed if ref in saved]
        if existing_heads:
            git(['fetch', '--no-tags', destination,
                 *[f'{ref}:refs/saved/{ref[5:]}' for ref in existing_heads]], env, deadline, repo)
        for ref in existing_heads:
            actual = git(['rev-parse', f'refs/saved/{ref[5:]}'], env, deadline, repo).strip()
            if actual != saved[ref]:
                raise SyncError('destination changed during fetch; retry on next run')
            try:
                git(['merge-base', '--is-ancestor', saved[ref], changed[ref]], env, deadline, repo)
            except SyncError:
                raise SyncError('upstream branch diverged; destination preserved') from None
        if private_key is not None:
            key_path = work / 'deploy-key'
            key_path.write_text(private_key)
            key_path.chmod(0o600)
            if not known_hosts or not Path(known_hosts).is_file():
                raise SyncError('pinned SSH host keys are missing')
            env['GIT_SSH_COMMAND'] = shlex.join([
                'ssh', '-F', '/dev/null', '-i', str(key_path),
                '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
                '-o', 'IdentityAgent=none', '-o', 'StrictHostKeyChecking=yes',
                '-o', f'UserKnownHostsFile={Path(known_hosts).resolve()}',
                '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'ConnectTimeout=30',
            ])
        # No force refspecs or pruning: an atomic push rejects the entire update on conflict.
        git(['push', '--atomic', push_url or destination,
             *[f'{sha}:{ref}' for ref, sha in changed.items()]], env, deadline, repo)
        verified = refs(destination, env, deadline)
        if any(verified.get(ref) != sha for ref, sha in changed.items()):
            raise SyncError('post-push verification failed; inspect destination before retrying')
        return {'state': 'updated', 'updated_refs': len(changed), 'upstream_refs': len(upstream)}


def load_config(path, key_json):
    entries = json.loads(Path(path).read_text())
    keys = json.loads(key_json)
    if not isinstance(entries, list) or not entries or not isinstance(keys, dict):
        raise ValueError('invalid repository or key configuration')
    # shortcut: 40 repos at four workers fit within the workflow's 120-minute limit; shard beyond 40.
    if len(entries) > 40:
        raise ValueError('repository limit exceeded; shard the workflow before adding more')
    names = set()
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError('invalid repository entry')
        name = item.get('name', '')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}', name):
            raise ValueError('invalid repository name')
        if name.lower() in names or item.get('upstream') != f'storytold/{name}':
            raise ValueError('duplicate repository or unexpected upstream')
        names.add(name.lower())
        key = keys.get(name)
        if not isinstance(key, str) or not key.startswith('-----BEGIN OPENSSH PRIVATE KEY-----\n'):
            raise ValueError('missing or invalid deploy key')
    if set(keys) != {item['name'] for item in entries}:
        raise ValueError('deploy key scope does not match repository list')
    return entries, keys


def write_report(path, report):
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(report, indent=2) + '\n')
    temporary.replace(path)


def main():
    root = Path(__file__).resolve().parent
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'repositories': []}
    failed = False
    try:
        entries, keys = load_config(root / 'repositories.json', os.environ.get('SYNC_DEPLOY_KEYS', '{}'))
    except (ValueError, OSError, TypeError):
        report['error'] = 'invalid repository list or deploy-key configuration'
        failed = True
        entries, keys = [], {}
    report['repositories'] = [
        {'name': item['name'], 'upstream': item['upstream'], 'state': 'pending'}
        for item in entries
    ]
    write_report(root / 'status.json', report)
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = {
            pool.submit(
                sync_repository,
                f'https://github.com/{item["upstream"]}.git',
                f'https://github.com/sourceharbor/{item["name"]}.git',
                f'git@github.com:sourceharbor/{item["name"]}.git', keys[item['name']],
                root / 'github_known_hosts', timeout=600,
            ): index for index, item in enumerate(entries)
        }
        for job in as_completed(jobs):
            index = jobs[job]
            try:
                result = job.result()
            except SyncError as error:
                result = {'state': 'failed', 'error': str(error)}
                failed = True
            except Exception:
                result = {'state': 'failed', 'error': 'unexpected local sync error; inspect configuration'}
                failed = True
            report['repositories'][index].update(result)
            write_report(root / 'status.json', report)
            print(f'{entries[index]["name"]}: {result["state"]}', flush=True)
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
