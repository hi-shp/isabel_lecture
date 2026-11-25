"""Provide the selected/last-known approximate position to VRS only, never /gps/fix."""
import json
import math
import urllib.request


def select_seed(state,last=None):
    p=state.get('position')
    if p and p.get('valid') and all(isinstance(p.get(k),(int,float)) and math.isfinite(p[k]) for k in ('lat','lon')):
        return dict(lat=p['lat'],lon=p['lon'],source='manual' if state.get('mode')=='simulated' else 'gps')
    return last


def main(args=None):
    import rclpy
    from rclpy.node import Node
    from sensor_msgs.msg import NavSatFix
    class Seed(Node):
        def __init__(self):
            super().__init__('water_survey_ntrip_seed');self.last=None
            self.pub=self.create_publisher(NavSatFix,'/water_survey/ntrip_fix',10)
            self.create_timer(1,self.tick)
        def tick(self):
            try:
                with urllib.request.urlopen('http://127.0.0.1:8000/api/state',timeout=.5) as r:
                    self.last=select_seed(json.load(r),self.last)
            except (OSError,ValueError):pass
            if not self.last:return
            msg=NavSatFix();msg.header.stamp=self.get_clock().now().to_msg()
            msg.header.frame_id='vrs_seed_'+self.last['source'];msg.status.status=0
            msg.latitude=self.last['lat'];msg.longitude=self.last['lon'];self.pub.publish(msg)
    rclpy.init(args=args);node=Seed()
    try:rclpy.spin(node)
    except KeyboardInterrupt:pass
    finally:node.destroy_node();rclpy.try_shutdown()


if __name__=='__main__':main()
