import base64, io, json, subprocess, unittest
from pathlib import Path
import secrets, random

import server

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = r"""
const zxing=require('@zxing/library'),{PNG}=require('pngjs');
const pure=process.argv[1]==='pure';
const list=JSON.parse(require('fs').readFileSync(0,'utf8'));
let ok=0,bad=0;
for(const [b64,expected] of list){
  const png=PNG.sync.read(Buffer.from(b64,'base64'));
  const lum=new Uint8ClampedArray(png.width*png.height);
  for(let i=0;i<lum.length;i++)lum[i]=(png.data[i*4]*299+png.data[i*4+1]*587+png.data[i*4+2]*114)/1000;
  const bmp=new zxing.BinaryBitmap(new zxing.HybridBinarizer(new zxing.RGBLuminanceSource(lum,png.width,png.height)));
  try{
    const hints=pure?new Map([[zxing.DecodeHintType.PURE_BARCODE,true]]):undefined;
    ok+= new zxing.QRCodeReader().decode(bmp,hints).getText()===expected?1:0;
  }catch(e){bad++}
}
console.log(JSON.stringify({ok,bad}));
"""


def run(mode, items):
    out = subprocess.run(["node", "-e", SCRIPT, mode], cwd=ROOT, input=json.dumps(items).encode(),
                         capture_output=True, timeout=120, check=True)
    return json.loads(out.stdout)


class QrGenerationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rng = random.Random(20260101)
        cls.items = []
        for _ in range(150):
            payload = "DEMO-ONLY:" + "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_") for _ in range(22))
            png = server.RequestHandler._generate_qr_png(payload)
            png = base64.b64decode(png)
            cls.items.append([base64.b64encode(png).decode(), payload])

    def test_every_generated_qr_decodes_exactly_in_pure_mode(self):
        result = run("pure", self.items)
        self.assertEqual(result, {"ok": len(self.items), "bad": 0})

    def test_detector_path_failure_rate_is_bounded_and_documented(self):
        # zxing-js's detector (not our encoder) misses some clean QRs; an independent
        # decoder (zxing-cpp) read 200/200. This guards against the rate getting worse.
        result = run("detect", self.items)
        self.assertLessEqual(result["bad"] / len(self.items), 0.15, result)


if __name__ == "__main__":
    unittest.main()
