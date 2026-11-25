"""River-centre guidance with live-editable obstacle-triggered zigzag legs."""
import math
import time


def heading_error(target, current):
    return (target-current+180) % 360-180


def trimmed_mean(values):
    """Drop floor(N * .2) values from EACH tail, then average the remainder."""
    values=sorted(values)
    trim=len(values)//5
    kept=values[trim:len(values)-trim] if trim else values
    return (sum(kept)/len(kept) if kept else None), len(kept)


def forward_scan(ranges, angle_min, angle_increment, range_min, range_max, yaw_offset_deg=0.):
    """180 one-degree sectors; centred +/-0.5 degree front decision window.

    The centred decision window avoids choosing only the left or right half of
    the bow at the 0-degree boundary of the [-90,90) display sectors.
    +inf is a delivered ray with no return, never a numeric hit in the mean.
    """
    if (len(ranges)==0 or len(ranges)>50000 or not all(math.isfinite(v) for v in
        (angle_min,angle_increment,range_min,range_max,yaw_offset_deg)) or
        angle_increment==0 or not 0<=range_min<range_max):
        raise ValueError('라이다 스캔 형식 오류')
    sectors=[[] for _ in range(180)];front=[];front_usable=0;front_rays=0
    for index, raw in enumerate(ranges):
        angle=heading_error(math.degrees(angle_min+index*angle_increment)+yaw_offset_deg,0.)
        if not -90<=angle<90:continue
        value=float(raw);is_front=-.5<=angle<.5
        if is_front:front_rays+=1
        if math.isinf(value) and value>0:
            if is_front:front_usable+=1
            continue
        if not math.isfinite(value) or value<=0 or not range_min<=value<=range_max:continue
        sectors[min(179,int(math.floor(angle+90)))].append(value)
        if is_front:front.append(value);front_usable+=1
    means=[];counts=[]
    for values in sectors:
        mean,count=trimmed_mean(values);means.append(mean);counts.append(count)
    mean,count=trimmed_mean(front)
    valid=front_rays>0 and front_usable>0
    return dict(valid=valid,front_mean_m=mean,front_hits=len(front),front_kept=count,
        front_rays=front_rays,front_interval_deg=[-.5,.5],trim_each_tail=.2,
        sector_start_deg=-90,sector_width_deg=1,sector_means_m=means,sector_kept=counts,
        obstacle=bool(valid and mean is not None and mean<=2.))


class RiverZigzag:
    """Keep the requested goal offset; alternate only on operator requests."""
    def __init__(self, clock=time.monotonic):
        self.side=1;self.course=None;self.waypoint=None;self.last_scan=None
        self.bank_armed=True;self.switches=0
        self.offset=None;self.threshold=None
        self.clock=clock;self.last_turn=-math.inf
        self.started=False
        self.pending_turns=0

    def request_turn(self):
        self.pending_turns+=1

    def guide(self, goal_bearing, heading, waypoint, scan,offset_deg=45.,obstacle_m=2.,turn_interval_s=3.):
        event=None
        if self.threshold!=obstacle_m:self.last_scan=None
        if not self.started:
            right=abs(heading_error(goal_bearing+offset_deg,heading))
            left=abs(heading_error(goal_bearing-offset_deg,heading))
            self.side=-1 if left<right else 1
            self.started=True
        self.waypoint=waypoint
        # Keep exactly the operator's offset as the goal bearing changes. A
        # latched absolute course would widen the offset near the destination.
        self.course=(goal_bearing+offset_deg*self.side)%360
        self.offset=offset_deg;self.threshold=obstacle_m
        if self.pending_turns:
            if self.pending_turns%2:self.side*=-1
            self.switches+=self.pending_turns;self.pending_turns=0
            self.course=(goal_bearing+offset_deg*self.side)%360;event='manual_turn'
        turning=abs(heading_error(self.course,heading))>10
        phase='direct' if offset_deg==0 else ('turn_' if turning else 'straight_')+('right' if self.side>0 else 'left')
        return dict(course_deg=self.course,goal_bearing_deg=goal_bearing,phase=phase,
            side=self.side,offset_deg=offset_deg*self.side,turn_trigger='keyboard_plus',
            bank_switches=self.switches,
            front_mean_m=scan['front_mean_m'],event=event)
