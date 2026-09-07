#!/usr/bin/env python3
import unittest

from benchmark_edge_disk_cache import evaluate_case


class EvaluateCaseTest(unittest.TestCase):
    def test_prometheus_external_hit_is_sufficient_when_usage_omits_details(
        self,
    ) -> None:
        cold = {"marker_ok": True, "ttft_sec": 13.2}
        fillers = [{"marker_ok": True}]
        disk_reload = {
            "marker_ok": True,
            "ttft_sec": 2.0,
            "cached_tokens": 0,
            "metric_delta": {
                'vllm:external_prefix_cache_hits_total{engine="0"}': 17600,
                'vllm:prompt_tokens_cached_total{engine="0"}': 17600,
            },
        }

        result = evaluate_case(cold, fillers, disk_reload)

        self.assertTrue(result["passed"])
        self.assertEqual(result["external_hit_tokens"], 17600)
        self.assertEqual(result["observed_cached_tokens"], 17600)
        self.assertEqual(result["ttft_reduction_ratio"], 0.8485)

    def test_external_hit_is_required(self) -> None:
        cold = {"marker_ok": True, "ttft_sec": 13.2}
        disk_reload = {
            "marker_ok": True,
            "ttft_sec": 2.0,
            "cached_tokens": 19200,
            "metric_delta": {
                'vllm:prompt_tokens_cached_total{engine="0"}': 19200,
            },
        }

        result = evaluate_case(cold, [], disk_reload)

        self.assertFalse(result["passed"])

    def test_ttft_reduction_threshold_is_required(self) -> None:
        cold = {"marker_ok": True, "ttft_sec": 10.0}
        disk_reload = {
            "marker_ok": True,
            "ttft_sec": 6.0,
            "cached_tokens": 0,
            "metric_delta": {
                'vllm:external_prefix_cache_hits_total{engine="0"}': 17600,
            },
        }

        result = evaluate_case(cold, [], disk_reload)

        self.assertFalse(result["passed"])
        self.assertEqual(result["ttft_reduction_ratio"], 0.4)


if __name__ == "__main__":
    unittest.main()
