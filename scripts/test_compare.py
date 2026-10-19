from collections import Counter
import unittest
import compare

class ComparisonOrderTests(unittest.TestCase):
    def test_each_environment_receives_each_position_in_four_rounds(self):
        modes=['legacy','k3s','bluegreen','rolling']
        rounds=[compare.round_order(modes,n) for n in range(1,5)]
        for position in range(4):self.assertEqual({r[position] for r in rounds},set(modes))
        predecessors=Counter((r[i],r[i+1]) for r in rounds for i in range(3))
        self.assertEqual(len(predecessors),12);self.assertTrue(all(n==1 for n in predecessors.values()))
    def test_ten_rounds_keep_exact_quota_without_adjacent_reuse(self):
        modes=['legacy','k3s','bluegreen','rolling'];schedule=[m for n in range(1,11) for m in compare.round_order(modes,n)]
        self.assertEqual(Counter(schedule),Counter({m:10 for m in modes}))
        self.assertTrue(all(a!=b for a,b in zip(schedule,schedule[1:])))

if __name__=='__main__':unittest.main()
