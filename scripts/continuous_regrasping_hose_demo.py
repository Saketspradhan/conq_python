import argparse
import time
from functools import partial
from typing import Callable

import bosdyn.client
import bosdyn.client.util
import numpy as np
import rerun as rr
from bosdyn import geometry
from bosdyn.api import geometry_pb2, ray_cast_pb2, arm_command_pb2, robot_command_pb2
from bosdyn.api import manipulation_api_pb2
from bosdyn.api.basic_command_pb2 import RobotCommandFeedbackStatus
from bosdyn.api.spot import robot_command_pb2 as spot_command_pb2
from bosdyn.client import math_helpers
from bosdyn.client.frame_helpers import ODOM_FRAME_NAME, GRAV_ALIGNED_BODY_FRAME_NAME, \
    GROUND_PLANE_FRAME_NAME, HAND_FRAME_NAME, get_a_tform_b, get_se2_a_tform_b, BODY_FRAME_NAME
from bosdyn.client.image import ImageClient, pixel_to_camera_space
from bosdyn.client.lease import LeaseKeepAlive, LeaseClient
from bosdyn.client.manipulation_api_client import ManipulationApiClient
from bosdyn.client.ray_cast import RayCastClient
from bosdyn.client.robot_command import RobotCommandBuilder, RobotCommandClient
from bosdyn.client.robot_command import (block_for_trajectory_cmd)
from bosdyn.client.robot_state import RobotStateClient
from google.protobuf import wrappers_pb2

from arm_segmentation.predictor import Predictor
from conq.cameras_utils import rot_2d, get_color_img, get_depth_img, camera_space_to_pixel, pos_in_cam_to_pos_in_hand
from conq.exceptions import DetectionError, GraspingException
from conq.manipulation import block_for_manipulation_api_command, open_gripper, force_measure, \
    do_grasp, raise_hand, add_follow_with_body
from conq.manipulation import blocking_arm_command
from conq.perception import get_gpe_in_cam, project_points
from conq.utils import setup
from regrasping_demo import homotopy_planner
from regrasping_demo.cdcpd_hose_state_predictor import single_frame_planar_cdcpd
from regrasping_demo.center_object import center_object_step
from regrasping_demo.detect_regrasp_point import min_angle_to_x_axis, detect_regrasp_point_from_hose
from regrasping_demo.get_detections import GetRetryResult, np_to_vec2, get_hose_and_regrasp_point, get_object_on_floor, \
    get_hose_and_head_point
from regrasping_demo.get_detections import save_data
from regrasping_demo.homotopy_planner import get_obstacle_coms

HOME = math_helpers.SE2Pose(0, 0, 0)


def hand_pose_cmd(robot_state_client, x, y, z, roll=0., pitch=np.pi / 2, yaw=0., duration=0.5):
    """
    Move the arm to a pose relative to the body

    Args:
        robot_state_client: RobotStateClient
        x: x position in meters in front of the body center
        y: y position in meters to the left of the body center
        z: z position in meters above the body center
        roll: roll in radians
        pitch: pitch in radians
        yaw: yaw in radians
        duration: duration in seconds
    """
    transforms = robot_state_client.get_robot_state().kinematic_state.transforms_snapshot

    hand_pos_in_body = geometry_pb2.Vec3(x=x, y=y, z=z)

    euler = geometry.EulerZXY(roll=roll, pitch=pitch, yaw=yaw)
    quat_hand = euler.to_quaternion()

    body_in_odom = get_a_tform_b(transforms, ODOM_FRAME_NAME, GRAV_ALIGNED_BODY_FRAME_NAME)
    hand_in_body = geometry_pb2.SE3Pose(position=hand_pos_in_body, rotation=quat_hand)

    hand_in_odom = body_in_odom * math_helpers.SE3Pose.from_proto(hand_in_body)

    arm_command = RobotCommandBuilder.arm_pose_command(
        hand_in_odom.x, hand_in_odom.y, hand_in_odom.z, hand_in_odom.rot.w, hand_in_odom.rot.x,
        hand_in_odom.rot.y, hand_in_odom.rot.z, ODOM_FRAME_NAME, duration)
    return arm_command


def look_at_scene(command_client, robot_state_client, x=0.56, y=0.1, z=0.55, pitch=0., yaw=0., dx=0., dy=0., dpitch=0.,
                  dyaw=0.):
    look_cmd = hand_pose_cmd(robot_state_client, x + dx, y + dy, z, 0, pitch + dpitch, yaw + dyaw, duration=0.5)
    blocking_arm_command(command_client, look_cmd)


def get_point_f_retry(command_client, robot_state_client, rc_client, get_point_f: Callable, z: float,
                      pitch: float) -> GetRetryResult:
    look_at_scene(command_client, robot_state_client, z=z, pitch=pitch)
    spiral_gen = spiral_search(rc_client, spiral_steps=23)
    for _ in spiral_gen:  # this will just visualize the search without moving the robot
        pass
    spiral_gen = spiral_search(rc_client)
    while True:
        try:
            return get_point_f()
        except DetectionError:
            gaze_command = next(spiral_gen)
            blocking_arm_command(command_client, gaze_command)
            time.sleep(0.1)  # avoid motion blur


def spiral_search(rc_client, rr_viz: bool = True, a=1.1, b=0.25, spiral_end=7 * np.pi,
                  spiral_steps=35):
    """
    Args:
        rr_viz: Whether to visualize the spiral search in the rerun
        spiral_end: The end of the spiral in radians, 0 to pi. pi means pointing opposite the initial direction.
        spiral_steps: The number of steps in the spiral, larger means more small steps.
    """
    # move the hand in a spiral pattern to search for objects on the floor
    if rr_viz:
        rr.log_arrow('world/x', [0, 0, 0], [1, 0, 0], color=[1, 0, 0, 1.0], width_scale=0.01, timeless=True)
        rr.log_arrow('world/y', [0, 0, 0], [0, 1, 0], color=[0, 1, 0, 1.0], width_scale=0.01, timeless=True)
        rr.log_arrow('world/z', [0, 0, 0], [0, 0, 1], color=[0, 0, 1, 1.0], width_scale=0.01, timeless=True)

    # Figure out where is the robot currently looking in body frame, and offset from that
    response = rc_client.raycast(np.zeros(3), np.array([1, 0, 0]),
                                 [ray_cast_pb2.RayIntersection.Type.TYPE_GROUND_PLANE],
                                 min_distance=0.2, frame_name=HAND_FRAME_NAME)

    if len(response.hits) == 0:
        raise DetectionError("No raycast hits")

    hit = response.hits[0]
    gaze_x0 = hit.hit_position_in_hit_frame.x
    gaze_y0 = hit.hit_position_in_hit_frame.y

    points = []
    if rr_viz:
        rr.set_time_seconds('spiral', 0)

    for t in np.linspace(0, spiral_end, spiral_steps):
        if rr_viz:
            rr.set_time_seconds('spiral', t)

        # logarithmic spiral in the XY plane
        r = np.power(a, t)
        x = b * (r * np.cos(t) - 1)
        y = b * r * np.sin(t)
        z = 0

        gaze_x_in_body = x + gaze_x0
        gaze_y_in_body = y + gaze_y0

        p = np.array([gaze_x_in_body, gaze_y_in_body, z])
        points.append(p)

        if rr_viz:
            rr.log_point('spiral/p', p, color=[1, 1, 1, 1.0], radius=0.01)
            rr.log_line_strip('spiral/all', points, color=[1, 1, 1, 1.0])

        gaze_command = RobotCommandBuilder.arm_gaze_command(gaze_x_in_body, gaze_y_in_body, 0,
                                                            GRAV_ALIGNED_BODY_FRAME_NAME,
                                                            max_linear_vel=0.3, max_angular_vel=0.8, max_accel=0.2)

        follow_arm_command = RobotCommandBuilder.follow_arm_command()
        full_cmd = RobotCommandBuilder.build_synchro_command(follow_arm_command, gaze_command)
        yield full_cmd

    if rr_viz:
        rr.log_points('spiral', points, colors=[1, 1, 1, 1.0], radii=0.01, timeless=True)


def drag_rope_to_goal(robot_state_client, command_client, goal):
    """ Move the robot to a pose relative to the body while dragging the hose """
    force_buffer = []

    # Raise arm a bit
    raise_hand(command_client, robot_state_client, 0.1)

    # Create the se2 trajectory for the dragging motion
    walk_cmd_id = walk_to_pose_in_odom(command_client, goal, block=False, locomotion_hint=spot_command_pb2.HINT_CRAWL)

    # loop to check forces
    while True:
        feedback = command_client.robot_command_feedback(walk_cmd_id)
        mobility_feedback = feedback.feedback.synchronized_feedback.mobility_command_feedback
        if mobility_feedback.status != RobotCommandFeedbackStatus.STATUS_PROCESSING:
            print("Failed to reach goal.")
            return False
        traj_feedback = mobility_feedback.se2_trajectory_feedback
        if (traj_feedback.status == traj_feedback.STATUS_AT_GOAL and
                traj_feedback.body_movement_status == traj_feedback.BODY_STATUS_SETTLED):
            print("Arrived at dragging goal.")
            return True
        if force_measure(robot_state_client, command_client, force_buffer):
            time.sleep(1)  # makes the video look better in my opinion
            print("High force detected. Failed to reach goal.")
            return False
        time.sleep(0.25)


def walk_to_pose_in_initial_frame(command_client, initial_transforms, goal, block=True, crawl=False):
    """
    Non-blocking, returns the command id
    """
    goal_pose_in_odom = get_se2_a_tform_b(initial_transforms, ODOM_FRAME_NAME, GRAV_ALIGNED_BODY_FRAME_NAME) * goal
    if crawl:
        locomotion_hint = spot_command_pb2.HINT_CRAWL
    else:
        locomotion_hint = spot_command_pb2.HINT_AUTO
    se2_cmd_id = walk_to_pose_in_odom(command_client, goal_pose_in_odom, locomotion_hint, block)
    return se2_cmd_id


def walk_to_pose_in_odom(command_client, goal_pose_in_odom, locomotion_hint, block):
    se2_cmd = RobotCommandBuilder.synchro_se2_trajectory_command(goal_se2=goal_pose_in_odom.to_proto(),
                                                                 frame_name=ODOM_FRAME_NAME,
                                                                 locomotion_hint=locomotion_hint)
    se2_synchro_commnd = RobotCommandBuilder.build_synchro_command(se2_cmd)
    se2_cmd_id = command_client.robot_command(lease=None, command=se2_synchro_commnd, end_time_secs=time.time() + 999)
    if block:
        block_for_trajectory_cmd(command_client, se2_cmd_id)
    return se2_cmd_id


def hand_delta_in_body_frame(command_client, robot_state_client, dx, dy, dz, follow=True):
    transforms = robot_state_client.get_robot_state().kinematic_state.transforms_snapshot
    hand_in_body = get_a_tform_b(transforms, GRAV_ALIGNED_BODY_FRAME_NAME, HAND_FRAME_NAME)
    hand_pos_in_body = hand_in_body.position
    cmd = hand_pose_cmd(robot_state_client, hand_pos_in_body.x + dx, hand_pos_in_body.y + dy, hand_pos_in_body.z + dz)
    if follow:
        cmd = add_follow_with_body(cmd)
    blocking_arm_command(command_client, cmd)


def align_with_hose(command_client, robot_state_client, rc_client, get_point_f):
    pick_res = get_point_f_retry(command_client, robot_state_client, rc_client, get_point_f, z=0.5,
                                 pitch=np.deg2rad(85))
    hose_points = pick_res.hose_points
    best_idx = pick_res.best_idx

    # Compute the angle of the hose around the given point using finite differencing
    if best_idx == 0:
        angle1 = min_angle_to_x_axis(hose_points[best_idx] - hose_points[best_idx + 1])
        angle2 = min_angle_to_x_axis(hose_points[best_idx + 1] - hose_points[best_idx + 2])
    elif best_idx == len(hose_points) - 1:
        angle1 = min_angle_to_x_axis(hose_points[best_idx] - hose_points[best_idx - 1])
        angle2 = min_angle_to_x_axis(hose_points[best_idx - 1] - hose_points[best_idx - 2])
    else:
        angle1 = min_angle_to_x_axis(hose_points[best_idx] - hose_points[best_idx - 1])
        angle2 = min_angle_to_x_axis(hose_points[best_idx] - hose_points[best_idx + 1])

    angle = (angle1 + angle2) / 2
    # The angles to +X in pixel space are "flipped" because images are stored with Y increasing downwards
    angle = -angle

    if abs(angle) < np.deg2rad(15):
        print("Not rotating because angle is small")
        return pick_res, angle

    # This is the point we want to rotate around
    best_px = hose_points[best_idx]

    # convert to camera frame and ignore the Z. Assumes the camera is pointed straight down.
    best_pt_in_cam = np.array(pixel_to_camera_space(pick_res.rgb_res, best_px[0], best_px[1], depth=1.0))[:2]
    best_pt_in_hand = pos_in_cam_to_pos_in_hand(best_pt_in_cam)

    rotate_around_point_in_hand_frame(command_client, robot_state_client, best_pt_in_hand, angle)
    return pick_res, angle


def rotate_around_point_in_hand_frame(command_client, robot_state_client, pos: np.ndarray, angle: float):
    """
    Moves the body by `angle` degrees, and translate so that `pos` stays in the same place.

    Assumptions:
     - the hand and the body have aligned X axes

    Args:
        command_client: command client
        robot_state_client: robot state client
        pos: 2D position of the point to rotate around, in the hand frame
        angle: angle in radians to rotate the body by, around the Z axis
    """
    transforms = robot_state_client.get_robot_state().kinematic_state.transforms_snapshot
    hand_in_odom = get_se2_a_tform_b(transforms, ODOM_FRAME_NAME, HAND_FRAME_NAME)
    hand_in_body = get_se2_a_tform_b(transforms, GRAV_ALIGNED_BODY_FRAME_NAME, HAND_FRAME_NAME)
    body_in_hand = hand_in_body.inverse()  # NOTE: querying frames in opposite order returns None???
    body_pt_in_hand = np.array([body_in_hand.x, body_in_hand.y])
    rotated_body_pos_in_hand = rot_2d(angle) @ body_pt_in_hand + pos
    rotated_body_in_hand = math_helpers.SE2Pose(rotated_body_pos_in_hand[0], rotated_body_pos_in_hand[1],
                                                angle + body_in_hand.angle)
    goal_in_odom = hand_in_odom * rotated_body_in_hand
    se2_cmd = RobotCommandBuilder.synchro_se2_trajectory_command(goal_se2=goal_in_odom.to_proto(),
                                                                 frame_name=ODOM_FRAME_NAME,
                                                                 locomotion_hint=spot_command_pb2.HINT_CRAWL)
    se2_synchro_commnd = RobotCommandBuilder.build_synchro_command(se2_cmd)
    se2_cmd_id = command_client.robot_command(lease=None, command=se2_synchro_commnd,
                                              end_time_secs=time.time() + 999)
    block_for_trajectory_cmd(command_client, se2_cmd_id)


def retry_do_grasp(robot, command_client, robot_state_client, manipulation_api_client, rc_client, get_point_f):
    z = 0.4
    for _ in range(8):
        try:
            pick_res = get_point_f()
            success = do_grasp(command_client, manipulation_api_client, robot_state_client, pick_res.rgb_res,
                               pick_res.best_vec2)
            if success:
                return
        except DetectionError:
            pass

        # Move the arm towards the detected point
        # pick_res.best_vec2  # pixel_xy in the rgb image
        hand_vel = arm_command_pb2.ArmVelocityCommand.CartesianVelocity()
        hand_vel.frame_name = HAND_FRAME_NAME
        # move forward if the hand is too far back
        snapshot = robot_state_client.get_robot_state().kinematic_state.transforms_snapshot
        hand_in_gpe = get_a_tform_b(snapshot, GROUND_PLANE_FRAME_NAME, HAND_FRAME_NAME)
        if hand_in_gpe.z > 0.1:
            x_vel = 0.02
        else:
            x_vel = -0.02
        hand_vel.velocity_in_frame_name.x = x_vel
        dt = 0.5
        end_time_secs = time.time() + dt
        end_time = robot.time_sync.robot_timestamp_from_local_secs(end_time_secs)
        arm_velocity_command = arm_command_pb2.ArmVelocityCommand.Request(cartesian_velocity=hand_vel,
                                                                          end_time=end_time)
        full_cmd = robot_command_pb2.RobotCommand()
        full_cmd.synchronized_command.arm_command.arm_velocity_command.CopyFrom(arm_velocity_command)
        command_client.robot_command(full_cmd)
        time.sleep(dt)
        command_client.robot_command(RobotCommandBuilder.stop_command())

    open_gripper(command_client)
    raise GraspingException("Failed to grasp")


def center_obstacles(predictor, command_client, robot_state_client, image_client, motion_scale=0.0004):
    rng = np.random.RandomState(0)
    for _ in range(5):
        rgb_np, rgb_res = get_color_img(image_client, 'hand_color_image')
        depth_np, depth_res = get_depth_img(image_client, 'hand_depth_in_hand_color_frame')
        predictions = predictor.predict(rgb_np)
        save_data(rgb_np, depth_np, predictions)

        delta_px = center_object_step(rgb_np, predictions, rng)

        if delta_px is None:
            print("success!")
            break

        # FIXME: generalize this math/transform. This assumes that +x in image (column) is -Y in body, etc.
        # FIXME: should be using camera intrinsics here so motion scale makes more sense
        dx_in_body, dy_in_body = np.array([-delta_px[1], -delta_px[0]]) * motion_scale
        hand_delta_in_body_frame(command_client, robot_state_client, dx_in_body, dy_in_body, dz=0, follow=False)


def walk_to(robot_state_client, command_client, manipulation_api_client, rc_client, get_point_f):
    walk_to_res = get_point_f_retry(command_client, robot_state_client, rc_client, get_point_f, z=0.5,
                                    pitch=np.deg2rad(45))

    offset_distance = wrappers_pb2.FloatValue(value=0.80)
    walk_to_cmd = manipulation_api_pb2.WalkToObjectInImage(
        pixel_xy=walk_to_res.best_vec2,
        transforms_snapshot_for_camera=walk_to_res.rgb_res.shot.transforms_snapshot,
        frame_name_image_sensor=walk_to_res.rgb_res.shot.frame_name_image_sensor,
        camera_model=walk_to_res.rgb_res.source.pinhole,
        offset_distance=offset_distance)
    walk_to_request = manipulation_api_pb2.ManipulationApiRequest(walk_to_object_in_image=walk_to_cmd)
    walk_response = manipulation_api_client.manipulation_api_command(manipulation_api_request=walk_to_request)
    block_for_manipulation_api_command(manipulation_api_client, walk_response)


def go_to_goal(predictor, robot, command_client, robot_state_client, image_client, manipulation_api_client, rc_client,
               initial_transforms, goal: math_helpers.SE2Pose):
    while True:
        # Grasp the hose to DRAG
        _get_hose_head = partial(get_hose_and_head_point, predictor, image_client)
        walk_to(robot_state_client, command_client, manipulation_api_client, rc_client, _get_hose_head)
        align_with_hose(command_client, robot_state_client, rc_client, _get_hose_head)
        retry_do_grasp(robot, command_client, robot_state_client, manipulation_api_client, rc_client, _get_hose_head)

        goal_reached = drag_rope_to_goal(robot_state_client, command_client, goal)
        if goal_reached:
            break

        # setup to look for the hose, which we just dropped
        open_gripper(command_client)
        look_at_scene(command_client, robot_state_client, z=0.3, pitch=np.deg2rad(85))

        # Look for the hose and get the full estimate in base frame
        spiral_gen = spiral_search(rc_client)
        while True:
            gaze_command = next(spiral_gen)
            blocking_arm_command(command_client, gaze_command)
            time.sleep(0.25)

        # First just walk to in front of that point
        # TODO: run A* planner
        # offset_distance = wrappers_pb2.FloatValue(value=1.00)
        # walk_to_cmd = manipulation_api_pb2.WalkToObjectInImage(
        #     pixel_xy=walk_to_res.best_vec2,
        #     transforms_snapshot_for_camera=walk_to_res.rgb_res.shot.transforms_snapshot,
        #     frame_name_image_sensor=walk_to_res.rgb_res.shot.frame_name_image_sensor,
        #     camera_model=walk_to_res.rgb_res.source.pinhole,
        #     offset_distance=offset_distance)
        # walk_to_request = manipulation_api_pb2.ManipulationApiRequest(walk_to_object_in_image=walk_to_cmd)
        # walk_response = manipulation_api_client.manipulation_api_command(manipulation_api_request=walk_to_request)
        # block_for_manipulation_api_command(manipulation_api_client, walk_response)

        # Move the arm to get the hose unstuck
        for _ in range(3):
            align_with_hose(command_client, robot_state_client, rc_client,
                            partial(get_hose_and_regrasp_point, predictor, image_client, ideal_dist_to_obs=40))

            # Center the obstacles in the frame
            try:
                center_obstacles(predictor, command_client, robot_state_client, image_client)
            except DetectionError:
                print("Failed to center obstacles, retrying")
                continue

            rgb_np, rgb_res = get_color_img(image_client, 'hand_color_image')
            depth_np, depth_res = get_depth_img(image_client, 'hand_depth_in_hand_color_frame')
            predictions = predictor.predict(rgb_np)
            save_data(rgb_np, depth_np, predictions)

            _, obstacles_mask = get_obstacle_coms(predictions)
            if np.sum(obstacles_mask) == 0:
                walk_to_pose_in_initial_frame(command_client, initial_transforms, HOME)
                continue

            obstacle_mask_with_valid_depth = np.logical_and(obstacles_mask, depth_np.squeeze(-1) > 0)
            nearest_obs_to_hand = np.min(depth_np[np.where(obstacle_mask_with_valid_depth)]) / 1000

            try:
                hose_points = single_frame_planar_cdcpd(rgb_np, predictions)
            except DetectionError:
                walk_to_pose_in_initial_frame(command_client, initial_transforms, HOME)
                continue

            _, regrasp_px = detect_regrasp_point_from_hose(predictions, hose_points, ideal_dist_to_obs=70)
            regrasp_vec2 = np_to_vec2(regrasp_px)
            regrasp_x_in_cam, regrasp_y_in_cam, _ = pixel_to_camera_space(rgb_res, regrasp_px[0], regrasp_px[1],
                                                                          depth=1.0)
            regrasp_x, regrasp_y = pos_in_cam_to_pos_in_hand([regrasp_x_in_cam, regrasp_y_in_cam])

            # BEFORE we grasp, get the robot's position in image space
            transforms = robot_state_client.get_robot_state().kinematic_state.transforms_snapshot
            body_in_hand = get_a_tform_b(transforms, HAND_FRAME_NAME, GRAV_ALIGNED_BODY_FRAME_NAME)
            hand_in_gpe = get_a_tform_b(transforms, GROUND_PLANE_FRAME_NAME, HAND_FRAME_NAME)
            hand_to_floor = hand_in_gpe.z
            body_in_cam = np.array([-body_in_hand.y, -body_in_hand.z])
            robot_px = np.array(camera_space_to_pixel(rgb_res, body_in_cam[0], body_in_cam[1], hand_to_floor))

            _, place_px = homotopy_planner.plan(rgb_np, predictions, regrasp_px, robot_px)

            place_x_in_cam, place_y_in_cam, _ = pixel_to_camera_space(rgb_res, place_px[0], place_px[1], depth=1.0)
            place_x, place_y = pos_in_cam_to_pos_in_hand([place_x_in_cam, place_y_in_cam])

            # Compute the desired poses for the hand
            nearest_obs_height = hand_to_floor - nearest_obs_to_hand
            dplace_x = place_x - regrasp_x
            dplace_y = place_y - regrasp_y

            # Do the grasp
            success = do_grasp(command_client, manipulation_api_client, robot_state_client, rgb_res, regrasp_vec2)
            if success:
                break
        else:
            # Give up and reset
            walk_to_pose_in_initial_frame(command_client, initial_transforms, HOME)
            continue

        hand_delta_in_body_frame(command_client, robot_state_client, dx=0, dy=0, dz=nearest_obs_height + 0.2,
                                 follow=False)
        hand_delta_in_body_frame(command_client, robot_state_client, dx=dplace_x, dy=dplace_y, dz=0)
        # Move down to the floor
        transforms = robot_state_client.get_robot_state().kinematic_state.transforms_snapshot
        hand_in_gpe = get_a_tform_b(transforms, GROUND_PLANE_FRAME_NAME, HAND_FRAME_NAME)
        hand_delta_in_body_frame(command_client, robot_state_client, dx=0, dy=0, dz=-hand_in_gpe.z + 0.05)
        # Open the gripper
        open_gripper(command_client)
        blocking_arm_command(command_client, RobotCommandBuilder.arm_stow_command())

        # reset before trying again
        walk_to_pose_in_initial_frame(command_client, initial_transforms, HOME)


def find_goal(robot_state_client, image_client, command_client, rc_client, predictor):
    goal = math_helpers.SE2Pose(0, 0, 0)

    _get_goal = partial(get_object_on_floor, predictor, image_client, 'mess_mat')
    get_goal_res = get_point_f_retry(command_client, robot_state_client, rc_client, _get_goal, z=0.5,
                                     pitch=np.deg2rad(45))

    # Project into the ground plane
    cam2odom = get_a_tform_b(get_goal_res.rgb_res.shot.transforms_snapshot, get_goal_res.rgb_res.shot.frame_name_image_sensor, ODOM_FRAME_NAME)

    pixels = np.array([[get_goal_res.best_vec2.y, get_goal_res.best_vec2.x]])
    goal_xy_in_odom = project_points(pixels, get_goal_res.rgb_res, cam2odom)[:2, 0]
    print(goal_xy_in_odom)

    snapshot = robot_state_client.get_robot_state().kinematic_state.transforms_snapshot
    body_in_odom = get_se2_a_tform_b(snapshot, ODOM_FRAME_NAME, GRAV_ALIGNED_BODY_FRAME_NAME)
    print(body_in_odom)

    goal.x = goal_xy_in_odom[0]
    goal.y = goal_xy_in_odom[1]
    goal.angle = 0

    body_in_odom = get_a_tform_b(snapshot, ODOM_FRAME_NAME, GRAV_ALIGNED_BODY_FRAME_NAME)
    gpe_in_odom = get_a_tform_b(snapshot, ODOM_FRAME_NAME, GROUND_PLANE_FRAME_NAME)
    hand_in_odom = get_a_tform_b(snapshot, ODOM_FRAME_NAME, HAND_FRAME_NAME)
    rr_tform('body', body_in_odom)
    rr_tform('gpe', gpe_in_odom)
    rr_tform('hand', hand_in_odom)
    rr.log_point('goal', [goal_xy_in_odom[0], goal_xy_in_odom[1], 0], radius=0.25, color=[1, 0, 1, 1.])

    return goal


def rr_tform(child_frame: str, tform: math_helpers.SE3Pose):
    translation = np.array([tform.position.x, tform.position.y, tform.position.z])
    rot_mat = tform.rotation.to_matrix()
    rr.log_transform3d(f'world/{child_frame}', rr.TranslationAndMat3(translation, rot_mat))


def main():
    np.seterr(all='raise')
    np.set_printoptions(precision=3, suppress=True)
    parser = argparse.ArgumentParser()
    bosdyn.client.util.add_base_arguments(parser)
    rr.init("continuous_regrasping_hose_demo")
    rr.connect()

    predictor = Predictor('models/hose_regrasping.pth')

    # Creates client, robot, and authenticates, and time syncs
    sdk = bosdyn.client.create_standard_sdk('continuous_regrasping_hose_demo')
    robot = sdk.create_robot('192.168.80.3')
    bosdyn.client.util.authenticate(robot)
    robot.time_sync.wait_for_sync()

    assert robot.has_arm(), "Robot requires an arm to run this example."

    assert not robot.is_estopped(), "Robot is estopped. Please use an external E-Stop client, such as the" \
                                    " estop SDK example, to configure E-Stop."

    lease_client = robot.ensure_client(LeaseClient.default_service_name)
    robot_state_client = robot.ensure_client(RobotStateClient.default_service_name)
    manipulation_api_client = robot.ensure_client(ManipulationApiClient.default_service_name)
    image_client = robot.ensure_client(ImageClient.default_service_name)
    rc_client = robot.ensure_client(RayCastClient.default_service_name)

    lease_client.take()

    with (LeaseKeepAlive(lease_client, must_acquire=True, return_at_exit=True)):
        setup(robot)
        command_client = robot.ensure_client(RobotCommandClient.default_service_name)

        # open the hand, so we can see more with the depth sensor
        open_gripper(command_client)

        while True:
            # First detect the goal
            goal_in_odom = find_goal(robot_state_client, image_client, command_client, rc_client, predictor)
            print(f'goal={goal_in_odom}')

            # check that the goal isn't too far, since that's likely bug and could be unsafe
            if np.linalg.norm([goal_in_odom.x, goal_in_odom.y]) > 5.0:
                print(f"Goal is too far away: {goal_in_odom.x, goal_in_odom.y}")
                continue

            initial_transforms = robot_state_client.get_robot_state().kinematic_state.transforms_snapshot

            go_to_goal(predictor, robot, command_client, robot_state_client, image_client, manipulation_api_client,
                       rc_client, initial_transforms, goal_in_odom)

            open_gripper(command_client)
            blocking_arm_command(command_client, RobotCommandBuilder.arm_stow_command())

            # Go home, you're done!
            walk_to_pose_in_initial_frame(command_client, initial_transforms, math_helpers.SE2Pose(0, 0, 0))


if __name__ == '__main__':
    main()
