import unittest
from unittest.mock import Mock

from tools.image_style_benchmark import percentile, static_shape


class ImageStyleBenchmarkTest(unittest.TestCase):
    def test_percentile_interpolates_sorted_timings(self):
        self.assertEqual(percentile([40.0, 10.0, 20.0], 0.5), 20.0)
        self.assertEqual(percentile([10.0, 20.0], 0.95), 19.5)

    def test_static_shape_rejects_dynamic_or_empty_dimensions(self):
        fixed = [Mock(is_dynamic=False, get_length=lambda value=n: value) for n in (1, 3, 224, 224)]
        self.assertEqual(static_shape(fixed), (1, 3, 224, 224))
        fixed[2].is_dynamic = True
        with self.assertRaises(ValueError):
            static_shape(fixed)


if __name__ == "__main__":
    unittest.main()
