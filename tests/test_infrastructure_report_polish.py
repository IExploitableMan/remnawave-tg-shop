import json
import unittest
from collections import Counter
from bot.services.infrastructure.diagnostics import diagnostic_plan
from bot.services.infrastructure.report_data import parse_result, result_measurements
from bot.services.infrastructure.report_summary import display_output
from bot.services.infrastructure.flags import flag_positions


class InfrastructureReportPolishTests(unittest.TestCase):
    def test_repeat_plan_runs_identity_once_but_measurements_each_pass(self):
        plan = diagnostic_plan('large', 3)
        counts = Counter(test[0] for iteration, test in plan)
        for name in ('ipregion', 'geo', 'dpi', 'tlab_censor', 'ipcheck', 'ipv6'):
            self.assertEqual(counts[name], 1)
        for name in ('ru_speed', 'yabs', 'udp', 'sustained', 'dns', 'cpu'):
            self.assertEqual(counts[name], 3)
        self.assertNotIn('ipquality', counts)  # same script/flags already covered by ipcheck
        self.assertTrue(all(round_number == 1 for round_number, test in diagnostic_plan('medium', 5)))

    def test_upload_is_retained_when_download_fails(self):
        result = {'id': 'bench', 'output': 'Kazan     3274 Mbits/sec     Download failed'}
        row = parse_result(result)['bars'][0]
        self.assertEqual(row['upload'], 3274)
        self.assertIsNone(row['value'])
        self.assertEqual(row['download_status'], 'Download failed')
        self.assertEqual(result_measurements(result), {'Kazan upload [Mbps]': 3274})
        missing = parse_result({'id': 'bench', 'output': 'Kazan     Download failed'})['bars'][0]
        self.assertIsNone(missing['value'])
        self.assertIsNone(missing['upload'])
        reverse = {'id': 'bench', 'output': 'Kazan     Upload failed     1500 Mbits/sec'}
        row = parse_result(reverse)['bars'][0]
        self.assertEqual(row['value'], 1500)
        self.assertIsNone(row['upload'])

    def test_yabs_busy_does_not_duplicate_success_into_other_direction(self):
        row = parse_result({'id': 'yabs', 'output': 'Eranium | Amsterdam | 4.90 Gbits/sec | busy | 6.75 ms'})['bars'][0]
        self.assertEqual(row['upload'], 4900)
        self.assertIsNone(row['value'])
        self.assertEqual(row['download_status'], 'busy')

    def test_udp_summary_preserves_missing_values_and_errors(self):
        output = json.dumps({'error': 'receiver disconnected', 'start': {'test_start': {'protocol': 'UDP', 'duration': 15}},
            'end': {'sum_sent': {'bits_per_second': 100000000, 'seconds': 15, 'bytes': 1000},
                    'sum_received': {'bits_per_second': 90000000, 'lost_percent': 2.5,
                                     'lost_packets': 25, 'packets': 1000, 'jitter_ms': .3}}})
        text = display_output({'id': 'udp', 'output': output})
        self.assertIn('receiver disconnected', text)
        self.assertIn('100.00 Mbps', text)
        self.assertIn('90.00 Mbps', text)
        self.assertIn('2.50% (25 / 1000 packets)', text)
        self.assertIn('0.30 ms', text)
        self.assertIn('N/A', text)
        self.assertNotIn('"sum_sent"', text)

    def test_sustained_summary_retains_intervals_and_statistics(self):
        data = {'intervals': [{'sum': {'start': 0, 'end': 15, 'bits_per_second': 100000000}},
                              {'sum': {'start': 15, 'end': 30, 'bits_per_second': 50000000}}]}
        text = display_output({'id': 'sustained', 'output': json.dumps(data)})
        self.assertIn('Intervals', text)
        self.assertIn('50.00 / 75.00 / 100.00 Mbps', text)

    def test_dns_and_cdn_summaries_preserve_failures(self):
        dns = display_output({'id': 'dns', 'output': '1.1.1.1\n;; status: NOERROR\n;; Query time: 12 msec\n8.8.8.8\n;; no servers could be reached'})
        self.assertIn('NOERROR', dns)
        self.assertIn('12 ms', dns)
        self.assertIn('no servers could be reached', dns)
        cdn = display_output({'id': 'cdn', 'output': 'curl: (28) timeout\nhttps://a.test connect=0 tls=0 ttfb=0'})
        self.assertIn('timeout', cdn)
        self.assertIn('failed / no response', cdn)

    def test_flags_use_padding_without_changing_columns_or_unknown_statuses(self):
        flags = flag_positions('Google           DE            NL', 'ipregion')
        self.assertEqual([p.name for _, p in flags], ['de.png', 'nl.png'])
        self.assertEqual(flag_positions('Spotify Signup  Yes           No', 'ipregion'), [])
        self.assertEqual(flag_positions('CPU model       DE', 'bench'), [])
        self.assertEqual(flag_positions('GeoIP            N/A          Rate-limit', 'ipregion'), [])
