import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(importlib.util.find_spec('ansible'), 'Ansible is required')
class InfrastructurePlatformTests(unittest.TestCase):
    def run_check(self, **changes):
        facts = {'distribution': 'Ubuntu', 'distribution_version': '26.04',
                 'pkg_mgr': 'apt', 'service_mgr': 'systemd', 'architecture': 'x86_64'}
        facts.update(changes)
        tasks = Path('infra/ansible/tasks/platform.yml').resolve()
        play = [{'hosts': 'localhost', 'gather_facts': False, 'connection': 'local',
                 'vars': {'ansible_facts': facts}, 'tasks': [{'ansible.builtin.import_tasks': str(tasks)}]}]
        with tempfile.TemporaryDirectory(prefix='platform-test-') as directory:
            path = Path(directory) / 'play.json'
            path.write_text(json.dumps(play))
            return subprocess.run([sys.executable, '-m', 'ansible.cli.playbook', '-i', 'localhost,',
                str(path)], capture_output=True, text=True, timeout=30)

    def test_debian_ubuntu_releases_are_not_an_enumerated_allowlist(self):
        for distribution, version in [('Ubuntu', '24.04'), ('Ubuntu', '26.04'),
                                      ('Ubuntu', '26.10'), ('Debian', '12'), ('Debian', '14')]:
            with self.subTest(distribution=distribution, version=version):
                result = self.run_check(distribution=distribution, distribution_version=version)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_incompatible_host_capabilities_are_rejected(self):
        for facts in [{'distribution': 'Fedora'}, {'pkg_mgr': 'dnf'},
                      {'service_mgr': 'openrc'}, {'architecture': 'riscv64'}]:
            with self.subTest(facts=facts):
                result = self.run_check(**facts)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Requires Debian/Ubuntu', result.stdout)
