import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from threading import Event, Lock
import sync as sync_module

from sync import SyncError, isolated_env, load_config, refs, sync_repository


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = isolated_env(self.root)
        self.env.update(GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                        GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
        self.source = self.root / 'source'
        self.destination = self.root / 'destination.git'
        self.run_git('init', '-b', 'main', str(self.source))
        self.commit('initial')
        self.run_git('clone', '--bare', str(self.source), str(self.destination))

    def run_git(self, *args, cwd=None):
        return subprocess.check_output(['git', '-c', 'core.hooksPath=/dev/null', *args],
                                       cwd=cwd, env=self.env, stderr=subprocess.DEVNULL, text=True).strip()

    def commit(self, text):
        (self.source / 'file.txt').write_text(text)
        self.run_git('add', 'file.txt', cwd=self.source)
        self.run_git('commit', '-m', text, cwd=self.source)
        return self.run_git('rev-parse', 'HEAD', cwd=self.source)

    def saved(self):
        return refs(str(self.destination), self.env, time.monotonic() + 30)

    def sync(self):
        return sync_repository(str(self.source), str(self.destination), timeout=30)

    def test_unchanged(self):
        self.assertEqual(self.sync()['state'], 'unchanged')

    def test_fast_forward_new_branch_and_annotated_tag(self):
        sha = self.commit('next')
        self.run_git('branch', 'new', cwd=self.source)
        self.run_git('tag', '-a', 'v1', '-m', 'release', cwd=self.source)
        self.assertEqual(self.sync()['updated_refs'], 3)
        saved = self.saved()
        self.assertEqual(saved['refs/heads/main'], sha)
        self.assertEqual(saved['refs/heads/new'], sha)
        self.assertEqual(saved['refs/tags/v1'], self.run_git('rev-parse', 'refs/tags/v1', cwd=self.source))

    def test_divergence_rejects_whole_batch(self):
        old = self.saved()
        self.run_git('checkout', '--orphan', 'replacement', cwd=self.source)
        self.commit('unrelated')
        self.run_git('branch', '-f', 'main', 'HEAD', cwd=self.source)
        self.run_git('branch', 'new', cwd=self.source)
        with self.assertRaisesRegex(SyncError, 'diverged'):
            self.sync()
        self.assertEqual(self.saved(), old)

    def test_moved_tag_rejects_whole_batch(self):
        self.run_git('tag', 'v1', cwd=self.source)
        self.sync()
        old = self.saved()
        self.commit('next')
        self.run_git('tag', '-f', 'v1', cwd=self.source)
        with self.assertRaisesRegex(SyncError, 'moved an existing tag'):
            self.sync()
        self.assertEqual(self.saved(), old)

    def test_upstream_deletions_do_not_delete_saved_refs(self):
        self.run_git('branch', 'retained', cwd=self.source)
        self.run_git('tag', 'v1', cwd=self.source)
        self.sync()
        old = self.saved()
        self.run_git('branch', '-d', 'retained', cwd=self.source)
        self.run_git('tag', '-d', 'v1', cwd=self.source)
        self.assertEqual(self.sync()['state'], 'unchanged')
        self.assertEqual(self.saved(), old)

    def test_missing_source_preserves_destination(self):
        old = self.saved()
        with self.assertRaises(SyncError):
            sync_repository(str(self.root / 'missing'), str(self.destination), timeout=30)
        self.assertEqual(self.saved(), old)

    def test_config_rejects_other_owner_and_extra_keys(self):
        config = self.root / 'repositories.json'
        key = '-----BEGIN OPENSSH PRIVATE KEY-----\ntest\n'
        entry = {'name': 'example', 'upstream': 'storytold/example', 'reason': 'test'}
        config.write_text(json.dumps([entry]))
        self.assertEqual(load_config(config, json.dumps({'example': key}))[0], [entry])
        with self.assertRaises(ValueError):
            load_config(config, json.dumps({'example': key, 'other': key}))
        entry['upstream'] = 'other/example'
        config.write_text(json.dumps([entry]))
        with self.assertRaises(ValueError):
            load_config(config, json.dumps({'example': key}))

    def test_no_upstream_checkout_or_hooks(self):
        self.commit('new')
        marker = self.root / 'must-not-exist'
        hook = self.source / '.git' / 'hooks' / 'pre-push'
        hook.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 1\n')
        hook.chmod(0o700)
        self.sync()
        self.assertFalse(marker.exists())


class LifecycleTests(unittest.TestCase):
    def test_partial_failure_is_saved_while_other_repository_is_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = [{'name': name, 'upstream': f'storytold/{name}'} for name in ('broken', 'slow')]
            snapshots = []
            failure_saved = Event()
            real_write = sync_module.write_report

            def save(path, report):
                real_write(path, report)
                snapshots.append(json.loads(path.read_text()))
                if report['repositories'][0]['state'] == 'failed':
                    failure_saved.set()

            def run(source, *args, **kwargs):
                self.assertEqual(kwargs['timeout'], 600)
                self.assertTrue((root / 'status.json').is_file())
                if source.endswith('/broken.git'):
                    raise SyncError('source unavailable')
                self.assertTrue(failure_saved.wait(2), 'failure must be persisted before slow job completes')
                partial = json.loads((root / 'status.json').read_text())
                self.assertEqual([r['state'] for r in partial['repositories']], ['failed', 'pending'])
                return {'state': 'unchanged', 'updated_refs': 0}

            with patch.object(sync_module, '__file__', str(root / 'sync.py')), \
                 patch.object(sync_module, 'load_config', return_value=(entries, {'broken': '', 'slow': ''})), \
                 patch.object(sync_module, 'write_report', side_effect=save), \
                 patch.object(sync_module, 'sync_repository', side_effect=run):
                self.assertEqual(sync_module.main(), 1)
            self.assertEqual([r['state'] for r in snapshots[0]['repositories']], ['pending', 'pending'])
            self.assertTrue(snapshots[0]['checked_at'])
            self.assertEqual([r['state'] for r in snapshots[-1]['repositories']], ['failed', 'unchanged'])
            self.assertFalse((root / 'status.json.tmp').exists())

    def test_configuration_failure_replaces_stale_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'status.json').write_text('{"checked_at":"stale","repositories":[]}')
            with patch.object(sync_module, '__file__', str(root / 'sync.py')), \
                 patch.object(sync_module, 'load_config', side_effect=ValueError), \
                 patch.object(sync_module, 'sync_repository') as run:
                self.assertEqual(sync_module.main(), 1)
            run.assert_not_called()
            report = json.loads((root / 'status.json').read_text())
            self.assertNotEqual(report['checked_at'], 'stale')
            self.assertIn('error', report)

    def test_at_most_four_repositories_run_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            entries = [{'name': f'repo{i}', 'upstream': f'storytold/repo{i}'} for i in range(8)]
            lock = Lock()
            four_started = Event()
            active = maximum = 0

            def run(*args, **kwargs):
                nonlocal active, maximum
                with lock:
                    active += 1
                    maximum = max(maximum, active)
                    if active == 4:
                        four_started.set()
                if not four_started.wait(2):
                    raise SyncError('expected concurrent execution')
                with lock:
                    active -= 1
                return {'state': 'unchanged', 'updated_refs': 0}

            with patch.object(sync_module, '__file__', str(Path(tmp) / 'sync.py')), \
                 patch.object(sync_module, 'load_config', return_value=(entries, {i['name']: '' for i in entries})), \
                 patch.object(sync_module, 'sync_repository', side_effect=run):
                self.assertEqual(sync_module.main(), 0)
            self.assertEqual(maximum, 4)

    def test_more_than_forty_repositories_requires_sharding(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'repositories.json'
            config.write_text(json.dumps([{'name': f'repo{i}', 'upstream': f'storytold/repo{i}'} for i in range(41)]))
            with self.assertRaisesRegex(ValueError, 'limit exceeded'):
                load_config(config, '{}')


if __name__ == '__main__':
    unittest.main()
