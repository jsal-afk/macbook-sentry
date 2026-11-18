#!/usr/bin/env python3
"""Local person detection and boxed delivery copies; never modify source recordings."""
from pathlib import Path
import hashlib
from importlib.metadata import version
import os
import subprocess

MODEL_VERSION = '1.8.0'
WEIGHTS_SHA256 = 'd8d6b9ee57d4d0ed2b1f305163624712a0532cb7bce0c747317984fc5457440d'


def verify_weights(path):
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise FileNotFoundError('Local RF-DETR Nano weights required')
    with path.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != WEIGHTS_SHA256:
            raise ValueError('Nano weights do not match the pinned upstream checkpoint')


def no_download(*args, **kwargs):
    if kwargs.get('redownload') or not args or not Path(args[0]).is_file():
        raise RuntimeError('RF-DETR tried to fetch weights; runtime downloads are disabled')


class PersonModel:
    def __init__(self, config):
        weights = Path(config['person_weights'])
        verify_weights(weights)
        if version('rfdetr') != MODEL_VERSION:
            raise ValueError('Install the pinned RF-DETR release from INSTALL.md')
        os.environ.setdefault('HF_HUB_DISABLE_TELEMETRY', '1')
        os.environ['HF_HUB_OFFLINE'] = '1'
        os.environ['TRANSFORMERS_OFFLINE'] = '1'
        os.environ.setdefault('TOKENIZERS_PARALLELISM', 'false')
        os.environ.setdefault('OMP_NUM_THREADS', '2')
        os.environ.setdefault('VECLIB_MAXIMUM_THREADS', '2')
        import cv2
        import numpy as np
        from PIL import Image
        from rfdetr import RFDETRNano
        from rfdetr.assets.coco_classes import COCO_CLASSES
        from rfdetr.models import weights as weight_loader

        # 1.8.0 calls its downloader even for local absolute paths. Keep the
        # guarded local read but make every fallback inside the loader fail.
        weight_loader.download_pretrain_weights = no_download

        class OfflineNano(RFDETRNano):
            def maybe_download_pretrain_weights(self):
                no_download(str(weights))

        cv2.setNumThreads(1)
        self.cv, self.np, self.image = cv2, np, Image
        self.ffmpeg = config['ffmpeg']
        self.confidence = config.get('person_confidence', .45)
        self.model = OfflineNano(device='cpu', pretrain_weights=str(weights))
        self.model.optimize_for_inference(compile=False)
        if COCO_CLASSES[1] != 'person':
            raise RuntimeError('Unexpected RF-DETR person label mapping')

    def detect_rgb(self, data, width, height):
        pixels = self.np.frombuffer(data, dtype=self.np.uint8).reshape(height, width, 3)
        return self.detect(self.image.fromarray(pixels))

    def detect(self, image):
        width, height = image.size
        result = self.model.predict(image, threshold=self.confidence)
        boxes = []
        for box, score, label in zip(result.xyxy, result.confidence, result.class_id):
            if int(label) != 1:
                continue
            x1, y1, x2, y2 = box
            boxes.append({'xyxy': [max(0,int(x1)),max(0,int(y1)),min(width-1,int(x2)),min(height-1,int(y2))],
                          'confidence': float(score)})
        return boxes

    def annotate(self, source, output):
        return annotate(source, output, self.cv, self.ffmpeg,
                        lambda frame: self.detect(self.image.fromarray(self.cv.cvtColor(frame, self.cv.COLOR_BGR2RGB))))


class PersonGate:
    def __init__(self):
        self.hits = []
        self.last_seen = -100
        self.last_event = -100
        self.active = False

    def update(self, now, present):
        self.hits = [x for x in self.hits if now-x <= 2.5]
        if now-self.last_seen > 8:
            self.active = False
        if not present:
            return False
        self.last_seen = now
        self.hits.append(now)
        if len(self.hits) >= 2 and not self.active and now-self.last_event >= 20:
            self.active = True
            self.last_event = now
            return True
        return False


def annotate(source, output, cv, ffmpeg, detect):
    """Render boxes into a distinct derivative. Preserve the clean AAC bitstream."""
    if source.resolve() == output.resolve():
        raise ValueError('Cannot annotate the original in place')
    cap = cv.VideoCapture(str(source))
    fps = cap.get(cv.CAP_PROP_FPS)
    width, height = int(cap.get(cv.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv.CAP_PROP_FRAME_HEIGHT))
    if not cap.isOpened() or fps <= 0 or width <= 0 or height <= 0:
        cap.release()
        raise RuntimeError('Unable to open event source')
    cmd = [ffmpeg,'-v','error','-nostdin','-y','-f','rawvideo','-pixel_format','bgr24',
           '-video_size',f'{width}x{height}','-framerate',str(fps),'-i','pipe:0','-i',str(source),
           '-map','0:v:0','-map','1:a:0','-c:v','h264_videotoolbox','-b:v','1500k',
           '-realtime','1','-allow_sw','0','-pix_fmt','nv12','-color_range','tv',
           '-c:a','copy','-map_metadata','-1','-movflags','+faststart',str(output)]
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        interval = max(1, round(fps/5))
        boxes = ()
        frames = 0
        with proc.stdin as pipe:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if frames % interval == 0:
                    boxes = detect(frame)
                for box in boxes:
                    x1, y1, x2, y2 = box['xyxy']
                    cv.rectangle(frame, (x1,y1), (x2,y2), (50,220,80), 2)
                    cv.putText(frame, f"person {box['confidence']:.2f}", (x1,max(18,y1-8)),
                               cv.FONT_HERSHEY_SIMPLEX, .6, (50,220,80), 2, cv.LINE_AA)
                pipe.write(memoryview(frame))
                frames += 1
        if proc.wait(timeout=30) or not frames:
            raise RuntimeError('Annotated video encoder failed')
    finally:
        cap.release()
        if proc and proc.poll() is None:
            proc.kill()
            proc.wait()


def selfcheck():
    gate = PersonGate()
    assert not gate.update(10,True)
    assert gate.update(10.5,True)
    assert not gate.update(11,True)
    assert not gate.update(20,False)
    assert not gate.update(31,True)
    assert gate.update(31.5,True)
    print('person_gate_selfcheck=pass')


if __name__ == '__main__':
    selfcheck()
