"""Optional detector-stack regression with generated video and a fixed box."""
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

import cv2

from person_detector import annotate
from sentry import probe


class BoxedVideo(unittest.TestCase):
    def test_render_box_keep_clean_mp4_and_aac_packets(self):
        ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
        assert ffmpeg and ffprobe
        with tempfile.TemporaryDirectory(prefix='sentry-box-') as tmp:
            clean, boxed = Path(tmp)/'clean.mp4', Path(tmp)/'boxed.mp4'
            subprocess.run([ffmpeg, '-v', 'error', '-f', 'lavfi', '-i', 'color=black:s=320x180:r=15',
                            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '2',
                            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(clean)], check=True)
            original = hashlib.sha256(clean.read_bytes()).digest()
            annotate(clean, boxed, cv2, ffmpeg, lambda _: [{'xyxy': [90, 65, 225, 165], 'confidence': .91}])
            self.assertEqual(hashlib.sha256(clean.read_bytes()).digest(), original)
            probe({'ffprobe': ffprobe}, boxed)
            def audio_hash(path):
                return subprocess.check_output([ffmpeg, '-v', 'error', '-i', str(path), '-map', '0:a:0',
                                                '-c:a', 'copy', '-f', 'hash', '-hash', 'sha256', '-'])
            self.assertEqual(audio_hash(clean), audio_hash(boxed))
            pixels = subprocess.check_output([ffmpeg, '-v', 'error', '-i', str(boxed), '-frames:v', '1',
                                              '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'])
            self.assertEqual(len(pixels), 320*180*3)
            red, green, blue = pixels[3*(100*320+90):3*(100*320+90)+3]
            self.assertGreater(green, 130)
            self.assertGreater(green, max(red, blue)+65)
            self.assertLess(max(pixels[3*(140*320+160):3*(140*320+160)+3]), 20)


if __name__ == '__main__':
    unittest.main()
