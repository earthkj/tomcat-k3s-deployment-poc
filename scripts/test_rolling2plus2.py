import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import variants

class Rolling2Plus2Tests(unittest.TestCase):
    def run_roll(self,failure=None):
        self.live={'tomcat1','tomcat2'};self.running=set(self.live);self.drained=set();self.actions=[]
        def replace(mode,name,version):
            self.actions.append(('prepare',name))
            if failure=='prepare':raise RuntimeError('not ready')
            self.running.add(name);self.assertLessEqual(len(self.running),3)
        def activate(mode,names):
            self.assertGreaterEqual(len(names),2);self.assertLessEqual(len(names),3)
            self.assertTrue(set(names)<=self.running)
            self.live=set(names);self.actions.append(('activate',set(names)))
        def drain(mode,names):
            self.assertFalse(self.live&names);self.actions.append(('drain',set(names)))
            if failure=='drain':raise TimeoutError('busy')
            self.drained|=names
        def compose(mode,command,name):
            self.assertEqual(command,'stop');self.assertNotIn(name,self.live);self.assertIn(name,self.drained)
            self.running.remove(name);self.actions.append(('stop',name))
        def front(mode,names,version=None):
            self.assertEqual(set(names),self.live);self.actions.append(('verify',set(names),version))
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(variants,'directory',return_value=Path(folder)),patch.object(variants,'active',side_effect=lambda mode:set(self.live)),patch.object(variants,'replace',side_effect=replace),patch.object(variants,'activation',side_effect=activate),patch.object(variants,'drain',side_effect=drain),patch.object(variants,'compose',side_effect=compose),patch.object(variants,'api_ready'),patch.object(variants,'verify_front',side_effect=front),patch.object(variants,'stage'):
                if failure:
                    with self.assertRaises((RuntimeError,TimeoutError)):variants._roll('bluegreen','v2')
                    self.assertTrue((Path(folder)/'pending.json').exists())
                else:
                    variants._roll('bluegreen','v2');self.assertFalse((Path(folder)/'pending.json').exists())
    def test_replaces_one_at_a_time_with_two_serving_members(self):
        self.run_roll()
        self.assertEqual(self.running,{'tomcat3','tomcat4'})
        self.assertLess(self.actions.index(('stop','tomcat1')),self.actions.index(('prepare','tomcat4')))
        self.assertIn(('activate',{'tomcat2','tomcat3'}),self.actions)
        self.assertEqual(self.actions[-1],('verify',{'tomcat3','tomcat4'},'v2'))
    def test_failed_preparation_preserves_old_members(self):
        self.run_roll('prepare');self.assertEqual(self.live,{'tomcat1','tomcat2'});self.assertEqual(self.running,self.live)
    def test_failed_drain_preserves_process_and_stops_next_replacement(self):
        self.run_roll('drain');self.assertIn('tomcat1',self.running)
        self.assertNotIn(('prepare','tomcat4'),self.actions)
        self.assertEqual(self.live,{'tomcat2','tomcat3'})

if __name__=='__main__':unittest.main()
