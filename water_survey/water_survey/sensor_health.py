"""Read-only sensor health snapshots for a running dashboard, no control topics."""
import collections
import datetime as dt
import json
import math
from pathlib import Path
import time

from .river_navigation import forward_scan


def main(args=None):
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import NavSatFix,Imu,LaserScan

    class Health(Node):
        def __init__(self):
            super().__init__('water_survey_sensor_health')
            self.declare_parameter('output',str(Path(__file__).parent/'static/sensor-health.json'))
            self.declare_parameter('scan_topic','/scan');self.declare_parameter('scan_yaw_offset_deg',0.)
            self.declare_parameter('heading_reference','/home/ubuntu/water_survey_data/heading-reference.json')
            self.output=Path(self.get_parameter('output').value);self.values={}
            self.times={k:collections.deque(maxlen=1000) for k in ('gps','imu','lidar','rtcm')};self.counts=collections.Counter()
            self.rtcm_bytes=0
            self.create_subscription(NavSatFix,'/gps/fix',self.gps,qos_profile_sensor_data)
            self.create_subscription(Imu,'/imu',self.imu,qos_profile_sensor_data)
            self.create_subscription(LaserScan,self.get_parameter('scan_topic').value,self.lidar,qos_profile_sensor_data)
            try:
                from rtcm_msgs.msg import Message
                self.create_subscription(Message,'/rtcm',self.rtcm,10)
            except ImportError:pass
            self.create_timer(.25,self.write)

        def save(self,key,data):
            now=time.monotonic();self.times[key].append(now);self.counts[key]+=1
            self.values[key]=dict(data,received_mono=now)

        def gps(self,msg):
            valid=msg.status.status>=0 and math.isfinite(msg.latitude) and math.isfinite(msg.longitude) and not (msg.latitude==0 and msg.longitude==0)
            self.save('gps',dict(valid=valid,status=msg.status.status,
                lat=msg.latitude if math.isfinite(msg.latitude) else None,
                lon=msg.longitude if math.isfinite(msg.longitude) else None))

        def imu(self,msg):
            x,y,z,w=msg.orientation.x,msg.orientation.y,msg.orientation.z,msg.orientation.w
            valid=msg.orientation_covariance[0]!=-1 and all(math.isfinite(v) for v in (x,y,z,w)) and .8<=x*x+y*y+z*z+w*w<=1.2
            data=dict(valid=valid,heading_deg=None,roll_deg=None,pitch_deg=None)
            if valid:
                yaw=math.degrees(math.atan2(2*(w*z+x*y),1-2*(y*y+z*z)))
                data.update(heading_deg=(90-yaw)%360,roll_deg=math.degrees(math.atan2(2*(w*x+y*z),1-2*(x*x+y*y))),
                    pitch_deg=math.degrees(math.asin(max(-1.,min(1.,2*(w*y-z*x))))))
            self.save('imu',data)

        def lidar(self,msg):
            try:
                result=forward_scan(msg.ranges,msg.angle_min,msg.angle_increment,msg.range_min,msg.range_max,
                    self.get_parameter('scan_yaw_offset_deg').value)
                data={k:result[k] for k in ('valid','front_mean_m','front_hits','front_kept')}
                age=self.get_clock().now().nanoseconds/1e9-msg.header.stamp.sec-msg.header.stamp.nanosec/1e9
                data['valid']=data['valid'] and -.25<=age<=1
                data['rays']=len(msg.ranges)
            except (ValueError,TypeError,OverflowError):data=dict(valid=False,front_mean_m=None)
            self.save('lidar',data)

        def rtcm(self,msg):
            length=len(getattr(msg,'message',getattr(msg,'data',[])));self.rtcm_bytes+=length
            self.save('rtcm',dict(valid=True,bytes=length,total_bytes=self.rtcm_bytes))

        def write(self):
            now=time.monotonic();state={}
            for key,data in self.values.items():
                times=self.times[key]
                while times and now-times[0]>2:times.popleft()
                hz=(len(times)-1)/(times[-1]-times[0]) if len(times)>1 else 0.
                state[key]={k:v for k,v in data.items() if k!='received_mono'}
                state[key].update(age_s=now-data['received_mono'],hz=round(hz,1),count=self.counts[key])
            state['updated_utc']=dt.datetime.now(dt.timezone.utc).isoformat()
            if state.get('imu',{}).get('heading_deg') is not None:
                try:
                    ref=json.loads(Path(self.get_parameter('heading_reference').value).read_text())['reference_deg']
                    state['imu']['raw_heading_deg']=state['imu']['heading_deg']
                    state['imu']['heading_deg']=(state['imu']['heading_deg']-float(ref))%360
                    state['imu']['reference_deg']=ref
                except (OSError,ValueError,TypeError,KeyError):pass
            temp=self.output.with_suffix('.tmp');temp.write_text(json.dumps(state,allow_nan=False),encoding='utf-8');temp.replace(self.output)

    rclpy.init(args=args);node=Health()
    try:rclpy.spin(node)
    except KeyboardInterrupt:pass
    finally:node.destroy_node();rclpy.try_shutdown()


if __name__=='__main__':main()
