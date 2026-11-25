import tempfile
import unittest
from water_survey.core import Survey,utc,latlon
from water_survey.recording import Recorder
from water_survey.ntrip_seed import select_seed

class ReferenceTests(unittest.TestCase):
    def test_bringup_first_heading_is_north_then_tracks_relative_rotations(self):
        s=Survey();s.begin_heading_zero();self.assertIsNone(s.heading)
        s.imu_update(300,utc());self.assertEqual(s.heading,0)
        s.imu_update(320,utc());self.assertEqual(s.heading,20)
        s.imu_update(280,utc());self.assertEqual(s.heading,340)
        s.begin_heading_zero();s.imu_update(250,utc());self.assertEqual(s.heading,0)

    def test_new_map_preserves_current_manual_gps_and_clears_only_survey(self):
        with tempfile.TemporaryDirectory() as root:
            s=Survey(True,recorder=Recorder(root));p={'lat':35.1777,'lon':129.1198}
            s.configure('simulated',[latlon(100,0,p)],{});s.set_virtual(**p)
            s.new_map()
            self.assertEqual(s.mode,'simulated');self.assertEqual(s.position()['lat'],p['lat'])
            self.assertEqual(s.position()['lon'],p['lon']);self.assertEqual(s.route,[])
            self.assertEqual(s.cells,{})

    def test_vrs_seed_uses_selected_position_without_inventing_a_real_gps_fix(self):
        manual=dict(mode='simulated',position=dict(lat=35.,lon=129.,valid=True))
        seed=select_seed(manual);self.assertEqual(seed['source'],'manual')
        no_fix=dict(mode='real',position=dict(lat=None,lon=None,valid=False))
        self.assertEqual(select_seed(no_fix,seed),seed);self.assertFalse(no_fix['position']['valid'])
        actual=dict(mode='real',position=dict(lat=35.1,lon=129.1,valid=True))
        self.assertEqual(select_seed(actual,seed)['source'],'gps')

if __name__=='__main__':unittest.main()
