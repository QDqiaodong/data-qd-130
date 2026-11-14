
import copy,json,unittest,tempfile,subprocess,sys,hashlib
from pathlib import Path
from project_path.model import analyze
ROOT=Path(__file__).resolve().parents[1]
def sample():return json.loads((ROOT/'fixtures/input.json').read_text())
class Business(unittest.TestCase):
    def bad(self,d):
        with self.assertRaises((ValueError,TypeError)):analyze(d)
    def test_unknown_top_field(self):
        d=sample();d['unexpected']=1;self.bad(d)

    def test_critical(self):
        r=analyze(sample());self.assertEqual(r['project_duration'],8);self.assertEqual(r['one_critical_path'],['A','C','D']);a={x['id']:x for x in r['activities']};self.assertEqual((a['B']['total_float'],a['B']['free_float'],a['E']['total_float']),(2,2,6))
    def test_order(self):
        d=sample();d['tasks'].reverse();self.assertEqual(analyze(d),analyze(sample()))
    def test_cycle(self):
        d=sample();d['tasks'][0]['predecessors']=['D'];self.bad(d)
    def test_unknown(self):
        d=sample();d['tasks'][0]['predecessors']=['unknown'];self.bad(d)
    def test_zero_chain(self):
        d={'tasks':[{'id':'A','duration':0,'predecessors':[]},{'id':'B','duration':0,'predecessors':['A']}]};r=analyze(d);self.assertEqual(r['project_duration'],0);self.assertEqual(r['one_critical_path'],['A','B'])
    def test_negative(self):
        d=sample();d['tasks'][0]['duration']=-1;self.bad(d)
    def test_duplicate_predecessor(self):
        d=sample();d['tasks'][1]['predecessors']=['A','A'];self.bad(d)
    def test_empty(self):self.bad({'tasks':[]})

class Command(unittest.TestCase):
    def call(self,source,output):
        return subprocess.run([sys.executable,'-m','project_path.cli','--source',str(source),'--output',str(output)],capture_output=True,text=True)
    def test_installed_cli_replay(self):
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'out.json';src=ROOT/'fixtures/input.json';r=self.call(src,out);self.assertEqual(r.returncode,0,r.stderr);first=out.read_bytes();report=json.loads(first);self.assertEqual(report['source_sha256'],hashlib.sha256(src.read_bytes()).hexdigest());self.assertEqual(self.call(src,out).returncode,0);self.assertEqual(first,out.read_bytes())
    def test_failure_preserves_output(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'bad.json';out=Path(td)/'out.json';out.write_text('prior')
            for body in ['{}','{"x":1,"x":2}','NaN']:
                src.write_text(body);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(out.read_text(),'prior')
    def test_output_safety(self):
        with tempfile.TemporaryDirectory() as td:
            src=Path(td)/'in.json';src.write_bytes((ROOT/'fixtures/input.json').read_bytes());before=src.read_bytes();self.assertEqual(self.call(src,src).returncode,2);self.assertEqual(src.read_bytes(),before);out=Path(td)/'link.json';out.symlink_to(src);self.assertEqual(self.call(src,out).returncode,2);self.assertEqual(src.read_bytes(),before)
