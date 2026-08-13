from __future__ import annotations

import hashlib
import inspect
import io
import json
import tempfile
import unittest
from pathlib import Path

import appdock
from scripts import update_helper


class _Response:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode('utf-8')

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


def _release(tag: str, *, prerelease: bool, draft: bool = False):
    return {
        'tag_name': tag,
        'draft': draft,
        'prerelease': prerelease,
        'html_url': f'https://github.com/eWOOD29/appdock/releases/tag/{tag}',
        'body': tag,
        'assets': [
            {
                'name': 'appdock-windows.zip',
                'browser_download_url': f'https://github.com/eWOOD29/appdock/releases/download/{tag}/appdock-windows.zip',
                'size': 123,
            },
            {
                'name': 'SHA256SUMS.txt',
                'browser_download_url': f'https://github.com/eWOOD29/appdock/releases/download/{tag}/SHA256SUMS.txt',
                'size': 86,
            },
        ],
    }


class UpdateChannelTests(unittest.TestCase):
    def test_semver_orders_numbered_beta_prereleases_and_stable(self):
        self.assertLess(appdock.compare_semver('0.2.1-beta.1', '0.2.1-beta.2'), 0)
        self.assertLess(appdock.compare_semver('0.2.1-beta.9', '0.2.1-beta.10'), 0)
        self.assertLess(appdock.compare_semver('0.2.1-beta.10', '0.2.1'), 0)
        self.assertGreater(appdock.compare_semver('0.2.1-beta.1', '0.2.0'), 0)

    def test_stable_parser_rejects_prereleases_even_if_github_flag_is_wrong(self):
        with self.assertRaises(appdock.AppDockError):
            appdock.parse_release(_release('v0.2.1-beta.1', prerelease=True))
        disguised = _release('v0.2.1-beta.1', prerelease=False)
        with self.assertRaises(appdock.AppDockError):
            appdock.parse_release(disguised)

    def test_beta_parser_accepts_only_numbered_beta_prereleases(self):
        parsed = appdock.parse_release(_release('v0.2.1-beta.2', prerelease=True), channel='beta')
        self.assertEqual(parsed['version'], '0.2.1-beta.2')
        for payload in (
            _release('v0.2.1', prerelease=False),
            _release('v0.2.1-rc.1', prerelease=True),
            _release('v0.2.1-beta.2', prerelease=False),
            _release('v0.2.1-beta.2', prerelease=True, draft=True),
        ):
            with self.subTest(tag=payload['tag_name'], prerelease=payload['prerelease'], draft=payload['draft']):
                with self.assertRaises(appdock.AppDockError):
                    appdock.parse_release(payload, channel='beta')

    def test_checker_keeps_stable_latest_endpoint_unchanged(self):
        seen = []
        def opener(request, timeout=0):
            seen.append((request.full_url, timeout))
            return _Response(_release('v0.2.1', prerelease=False))
        checker = appdock.ReleaseChecker(opener=opener, current='0.2.0', cache_ttl=0)
        result = checker.check('stable')
        self.assertTrue(result['update_available'])
        self.assertEqual(result['channel'], 'stable')
        self.assertEqual(seen[0][0], 'https://api.github.com/repos/eWOOD29/appdock/releases/latest')

    def test_checker_selects_highest_valid_beta_and_ignores_drafts_and_other_prereleases(self):
        payload = [
            _release('v0.2.1-beta.2', prerelease=True),
            _release('v0.2.1', prerelease=False),
            _release('v0.2.1-rc.5', prerelease=True),
            _release('v0.2.1-beta.10', prerelease=True),
            _release('v0.2.2-beta.1', prerelease=True, draft=True),
        ]
        seen = []
        def opener(request, timeout=0):
            seen.append(request.full_url)
            return _Response(payload)
        checker = appdock.ReleaseChecker(opener=opener, current='0.2.0', cache_ttl=0)
        result = checker.check('beta')
        self.assertEqual(result['version'], '0.2.1-beta.10')
        self.assertTrue(result['available'])
        self.assertTrue(result['update_available'])
        self.assertEqual(result['channel'], 'beta')
        self.assertEqual(seen[0], 'https://api.github.com/repos/eWOOD29/appdock/releases?per_page=100')

    def test_beta_channel_without_prerelease_is_non_error_and_non_updating(self):
        def opener(_request, timeout=0):
            return _Response([_release('v0.2.1', prerelease=False)])
        result = appdock.ReleaseChecker(opener=opener, current='0.2.0', cache_ttl=0).check('beta')
        self.assertFalse(result['available'])
        self.assertFalse(result['update_available'])
        self.assertEqual(result['version'], '')

    def test_switching_back_to_stable_never_marks_older_stable_as_update(self):
        def opener(_request, timeout=0):
            return _Response(_release('v0.2.0', prerelease=False))
        result = appdock.ReleaseChecker(opener=opener, current='0.2.1-beta.3', cache_ttl=0).check('stable')
        self.assertFalse(result['update_available'])

    def test_update_channel_defaults_stable_and_persists_beta_outside_program_files(self):
        with tempfile.TemporaryDirectory() as td:
            config = appdock.AppDockConfig.from_environment(data_dir=td)
            self.assertEqual(appdock.read_update_channel(config), 'stable')
            self.assertEqual(appdock.write_update_channel(config, 'beta'), 'beta')
            self.assertEqual(appdock.read_update_channel(config), 'beta')
            settings = Path(td) / 'update-settings.json'
            self.assertEqual(json.loads(settings.read_text(encoding='utf-8')), {'schema_version': 1, 'channel': 'beta'})

    def test_invalid_or_corrupt_channel_fails_safe_to_stable(self):
        with tempfile.TemporaryDirectory() as td:
            config = appdock.AppDockConfig.from_environment(data_dir=td)
            path = Path(td) / 'update-settings.json'
            path.write_text('{"schema_version":1,"channel":"nightly"}', encoding='utf-8')
            self.assertEqual(appdock.read_update_channel(config), 'stable')
            path.write_text('{broken', encoding='utf-8')
            self.assertEqual(appdock.read_update_channel(config), 'stable')
            with self.assertRaises(appdock.AppDockError):
                appdock.write_update_channel(config, 'nightly')

    def test_ui_exposes_explicit_stable_beta_selector_and_warning(self):
        self.assertIn('id="updateChannel"', appdock.HTML)
        self.assertIn('<option value="stable">Stable</option>', appdock.HTML)
        self.assertIn('<option value="beta">Beta (pre-release)</option>', appdock.HTML)
        self.assertIn('Beta builds are optional prereleases', appdock.HTML)

    def test_http_surface_binds_check_and_stage_to_persisted_channel(self):
        source = Path(appdock.__file__).read_text(encoding='utf-8')
        self.assertIn('self.checker.check(read_update_channel(self.config))', source)
        self.assertIn('if path == "/api/updates/channel":', source)
        self.assertIn('write_update_channel(self.config, body.get("channel"))', source)
        self.assertIn('stage_coordinated_update(release, self.config, Handler.coordinator', source)


class UpdaterIncidentHardeningTests(unittest.TestCase):
    def _write_release_tree(self, root: Path):
        members = {
            'appdock.py': b'app',
            'static/app.js': b'js',
            'static/app.css': b'css',
            'scripts/update_helper.py': b'helper',
            'scripts/path_safety.ps1': b'safety',
            'scripts/install.ps1': b'install',
            'scripts/uninstall.ps1': b'uninstall',
        }
        for relative, payload in members.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        manifest = {
            'schema_version': 2,
            'files': [
                {'path': relative, 'sha256': hashlib.sha256(payload).hexdigest()}
                for relative, payload in sorted(members.items())
            ],
        }
        (root / 'RELEASE-MANIFEST.json').write_text(json.dumps(manifest), encoding='utf-8')

    def test_mixed_install_inventory_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            install = Path(td) / 'install'
            self._write_release_tree(install)
            extra = install / '.venv' / 'Lib' / 'site.py'
            extra.parent.mkdir(parents=True)
            extra.write_text('local file\n', encoding='utf-8')
            with self.assertRaises(appdock.AppDockError) as raised:
                appdock._validate_installed_tree(install)
            self.assertIn('unexpected unowned files', str(raised.exception))

    def test_helper_preflight_occurs_before_shutdown_handshake(self):
        source = inspect.getsource(update_helper.run)
        self.assertLess(source.index('_validate_installed_tree(install)'), source.index('temporary.replace(handshake)'))
        self.assertLess(source.index('temporary.replace(handshake)'), source.index('while _alive(pid)'))
        self.assertIn('AppDock was left running', source)

    def test_restart_launch_uses_durable_diagnostic_streams(self):
        source = inspect.getsource(update_helper._launch_and_wait)
        self.assertIn('stdout=stdout_target', source)
        self.assertIn('stderr=stderr_target', source)
        self.assertIn('RESTART_STDOUT_LOG_NAME', source)
        self.assertIn('RESTART_STDERR_LOG_NAME', source)
        self.assertIn('restart diagnostics were captured', source)
        self.assertIn('close_fds=True', source)

    def test_restart_log_stream_is_appendable_and_persistent(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'runtime' / update_helper.RESTART_STDOUT_LOG_NAME
            with update_helper._open_restart_log_stream(path) as stream:
                stream.write('first\n')
                stream.flush()
            with update_helper._open_restart_log_stream(path) as stream:
                stream.write('second\n')
                stream.flush()
            self.assertEqual(path.read_text(encoding='utf-8'), 'first\nsecond\n')


class BetaWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parents[1]

    def test_ci_runs_for_develop(self):
        workflow = (self.root / '.github/workflows/ci.yml').read_text(encoding='utf-8')
        self.assertIn('branches: [main, develop]', workflow)

    def test_stable_release_workflow_excludes_prerelease_tags(self):
        workflow = (self.root / '.github/workflows/release.yml').read_text(encoding='utf-8')
        self.assertIn("- '!v*-*'", workflow)

    def test_beta_release_is_prerelease_only_from_develop_ancestry(self):
        workflow = (self.root / '.github/workflows/beta-release.yml').read_text(encoding='utf-8')
        self.assertIn("- 'v*-beta.*'", workflow)
        self.assertIn('git merge-base --is-ancestor $env:GITHUB_SHA origin/develop', workflow)
        self.assertIn('--prerelease --latest=false', workflow)
        self.assertIn('--repo $env:GITHUB_REPOSITORY', workflow)
        publish = workflow.split('  publish:', 1)[1]
        self.assertNotIn('actions/checkout@', publish)
        self.assertNotIn('build_portable.py', publish)


if __name__ == '__main__':
    unittest.main()
