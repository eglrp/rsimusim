from __future__ import print_function, division

import os
from collections import namedtuple
import bisect

import h5py
import numpy as np
import crisp.rotations
import crisp.fastintegrate
from imusim.maths.quaternions import Quaternion, QuaternionArray
from imusim.utilities.time_series import TimeSeries
from imusim.trajectories.splined import \
    SplinedPositionTrajectory, SampledPositionTrajectory, \
    SampledRotationTrajectory, SplinedRotationTrajectory, \
    SampledTrajectory, SplinedTrajectory


class DatasetError(Exception):
    pass

class Landmark(object):
    __slots__ = ('id', '_color', 'position', 'visibility', '_observations')

    def __init__(self, _id, position, observations):
        self.id = _id
        self.position = position
        self._color = None
        self.visibility = None # Set by observation setter
        self._observations = observations

    def __repr__(self):
        return '<Landmark #{id:d} ({X[0]:.2f}, {X[1]:.2f}, {X[2]:.2f})>'.format(id=self.id, X=self.position)

    @property
    def observations(self):
        return self._observations

    @observations.setter
    def observations(self, obs):
        self._observations = obs
        self.visibility = set(obs.keys())

    @property
    def color(self):
        if self._color is None:
            return 255 * np.ones((4,), dtype='uint8')
        else:
            r, g, b = self._color[:3]
            a = 255 if self._color.size == 3 else self._color[-1]
            return np.array([r, g, b, a], dtype='uint8')


class Dataset(object):
    def __init__(self):
        self._position_data = None
        self._orientation_data = None
        self.trajectory = None
        self.landmarks = []
        self._landmark_bounds = None

    def position_from_nvm(self, nvm_model, frame_to_time_func=None, camera_fps=None):
        if (bool(camera_fps is None) == bool(frame_to_time_func is None)):
            raise DatasetError("Must specify frame_to_time_func OR camera_fps, not both or none of them")
        frame_time = frame_to_time_func if frame_to_time_func else lambda n: float(n) / camera_fps

        cameras = sorted(nvm_model.cameras, key=lambda c: c.framenumber)
        camera_times = np.array([frame_time(c.framenumber) for c in cameras])
        camera_pos = np.vstack([c.position for c in cameras]).T
        ts = TimeSeries(camera_times, camera_pos)
        self._position_data = ts

        self._update_trajectory()

    def position_from_openmvg(self, sfm_data, frame_to_time_func=None, camera_fps=None):
        if (bool(camera_fps is None) == bool(frame_to_time_func is None)):
            raise DatasetError("Must specify frame_to_time_func OR camera_fps, not both or none of them")
        frame_time = frame_to_time_func if frame_to_time_func else lambda n: float(n) / camera_fps
        views = sorted(sfm_data.views, key=lambda v: v.framenumber)
        view_times = np.array([frame_time(v.framenumber) for v in views])
        view_pos = np.vstack([v.c for v in views])
        ts = TimeSeries(view_times, view_pos)
        self._position_data = ts
        self._update_trajectory()

    def position_from_sfm(self, sfm):
        view_times = np.array([v.time for v in sfm.views])
        view_positions = np.vstack([v.position for v in sfm.views]).T
        ts = TimeSeries(view_times, view_positions)
        self._position_data = ts
        self._update_trajectory()

    def orientation_from_sfm(self, sfm):
        view_times = np.array([v.time for v in sfm.views])
        view_orientations = QuaternionArray([v.orientation for v in sfm.views])
        view_orientations = view_orientations.unflipped()
        # Resampling is important to get good splines
        view_orientations, view_times = resample_quaternion_array(view_orientations, view_times)
        ts = TimeSeries(view_times, view_orientations)
        self._orientation_data = ts
        self._update_trajectory()

    def orientation_from_nvm(self, nvm_model, frame_to_time_func=None, camera_fps=None):
        if (bool(camera_fps is None) == bool(frame_to_time_func is None)):
            raise DatasetError("Must specify frame_to_time_func OR camera_fps, not both or none of them")
        frame_time = frame_to_time_func if frame_to_time_func else lambda n: float(n) / camera_fps
        cameras = sorted(nvm_model.cameras, key=lambda c: c.framenumber)
        camera_times = np.array([frame_time(c.framenumber) for c in cameras])
        camera_orientations = QuaternionArray([c.orientation for c in cameras])
        camera_orientations = camera_orientations.unflipped()

        # Must resample to uniform sample time for splining to work
        camera_orientations, camera_times = resample_quaternion_array(camera_orientations, camera_times)
        ts = TimeSeries(camera_times, camera_orientations)
        self._orientation_data = ts
        self._update_trajectory()

    def orientation_from_openmvg(self, sfm_data, frame_to_time_func=None, camera_fps=None):
        if (bool(camera_fps is None) == bool(frame_to_time_func is None)):
            raise DatasetError("Must specify frame_to_time_func OR camera_fps, not both or none of them")
        frame_time = frame_to_time_func if frame_to_time_func else lambda n: float(n) / camera_fps
        views = sorted(sfm_data.views, key=lambda v: v.framenumber)
        view_times = np.array([frame_time(v.framenumber) for v in views])
        view_orientations = QuaternionArray([Quaternion.fromMatrix(v.R.T) for v in views])
        view_orientations = view_orientations.unflipped()

        # Must resample to uniform sample time for splining to work
        view_orientations, view_times = resample_quaternion_array(view_orientations, view_times)
        ts = TimeSeries(view_times, view_orientations)
        self._orientation_data = ts
        self._update_trajectory()

    def orientation_from_gyro(self, gyro_data, gyro_times):
        n, d = gyro_data.shape

        if d == 3:
            dt = float(gyro_times[1] - gyro_times[0])
            if not np.allclose(np.diff(gyro_times), dt):
                raise DatasetError("gyro timestamps must be uniformly sampled")

            qdata = crisp.fastintegrate.integrate_gyro_quaternion_uniform(gyro_data, dt)
            Q = QuaternionArray(qdata)
        elif d == 4:
            Q = QuaternionArray(gyro_data)
        else:
            raise DatasetError("Gyro data must have shape (N,3) or (N, 4), was {}".format(gyro_data.shape))

        ts = TimeSeries(gyro_times, Q.unflipped())
        self._orientation_data = ts
        self._update_trajectory()

    def landmarks_from_nvm(self, nvm_model, camera_fps):
        camera_times = [c.framenumber / camera_fps for c in nvm_model.cameras]
        remap = {old : new for new, old in enumerate(np.argsort(camera_times))}
        self._landmark_bounds = create_bounds(np.array(sorted(camera_times)))
        for p in nvm_model.points:
            vis = set([remap[v] for v in p.visibility])
            lm = Landmark(p.position, vis, color=p.color)
            self.landmarks.append(lm)

    def landmarks_from_openmvg(self, sfm_data, camera_fps):
        view_times = [v.framenumber / camera_fps for v in sfm_data.views]
        remap = {old : new for new, old in enumerate(np.argsort(view_times))}
        self._landmark_bounds = create_bounds(np.array(sorted(view_times)))
        for s in sfm_data.structure:
            visibility = set([remap[v] for v in s.observations.keys()])
            lm = Landmark(s.point, visibility, color=s.color)
            self.landmarks.append(lm)

    def landmarks_from_sfm(self, sfm):
        view_times = [view.time for view in sfm.views]
        if not np.all(np.diff(view_times) > 0):
            raise DatasetError("View times are not increasing monotonically with id")

        self._landmark_bounds = create_bounds(view_times)
        for lm in sfm.landmarks:
            self.landmarks.append(lm)

    def visible_landmarks(self, t):
        i = bisect.bisect_left(self._landmark_bounds, t)
        interval_id = i - 1
        return [lm for lm in self.landmarks if interval_id in lm.visibility]

    def save(self, filepath, name):
        if os.path.exists(filepath):
            raise DatasetError('File {} already exists'.format(filepath))
        with h5py.File(filepath, 'w') as h5f:
            def save_keyframes(ts, groupkey):
                group = h5f.create_group(groupkey)
                if isinstance(ts.values, QuaternionArray):
                    values = ts.values.array
                else:
                    values = ts.values
                group['data'] = values
                group['timestamps'] = ts.timestamps

            h5f.attrs['name'] = name

            save_keyframes(self._position_data, 'position')
            save_keyframes(self._orientation_data, 'orientation')

            landmarks_group = h5f.create_group('landmarks')
            landmarks_group['visibility_bounds'] = self._landmark_bounds
            num_digits = int(np.ceil(np.log10(len(self.landmarks) + 0.5))) # 0.5 to avoid boundary conditions
            positions = np.empty((len(self.landmarks), 3))
            colors = np.empty_like(positions, dtype='uint8')
            visibility_group = landmarks_group.create_group('visibility')
            for i, landmark in enumerate(self.landmarks):
                vgroup_key = '{:0{pad}d}'.format(i, pad=num_digits)
                visibility_group[vgroup_key] = np.array(list(landmark.visibility)).astype('uint64')
                positions[i] = landmark.position
                colors[i] = landmark.color[:3] # Skip alpha
            landmarks_group['positions'] = positions
            landmarks_group['colors'] = colors

    def visualize(self, draw_orientations=False, draw_axes=False):
        from mayavi import mlab
        t_min = self.trajectory.startTime
        t_max = self.trajectory.endTime
        t_samples = (t_max - t_min) * 50
        t = np.linspace(t_min, t_max, t_samples)
        positions = self.trajectory.position(t)
        landmark_data = np.vstack([lm.position for lm in self.landmarks]).T
        landmark_scalars = np.arange(len(self.landmarks))

        landmark_colors = np.vstack([lm.color for lm in self.landmarks])
        orientation_times = np.linspace(t_min, t_max, num=50)
        orientations = self.trajectory.rotation(orientation_times)

        # World to camera transform is
        # Xc = RXw - Rt where R is the camera orientation and position respectively
        # Camera to world is thus
        # Xw = RtXc + t
        zc = np.array([0, 0, 1.]).reshape(3,1)
        zw = [np.dot(np.array(q.toMatrix()).T, zc).reshape(3,1) for q in orientations]
        quiver_pos = self.trajectory.position(orientation_times)
        quiver_data = 0.5 * np.hstack(zw)

        pts = mlab.points3d(landmark_data[0], landmark_data[1], landmark_data[2],
                            landmark_scalars, scale_factor=0.2, scale_mode='none')
        pts.glyph.color_mode = 'color_by_scalar'
        pts.module_manager.scalar_lut_manager.lut.table = landmark_colors

        plot_obj = mlab.plot3d(positions[0], positions[1], positions[2], color=(1, 0, 0), line_width=5.0, tube_radius=None)
        if draw_axes:
            mlab.axes(plot_obj)
        if draw_orientations:
            mlab.quiver3d(quiver_pos[0], quiver_pos[1], quiver_pos[2],
                          quiver_data[0], quiver_data[1], quiver_data[2], color=(1, 1, 0))
        mlab.show()

    @classmethod
    def from_file(cls, filepath):
        instance = cls()

        def load_timeseries(group):
            timestamps = group['timestamps'].value
            data = group['data'].value
            if data.shape[1] == 4:
                data = QuaternionArray(data)
            return TimeSeries(timestamps, data)

        with h5py.File(filepath, 'r') as h5f:
            instance._position_data = load_timeseries(h5f['position'])
            instance._orientation_data = load_timeseries(h5f['orientation'])
            instance._update_trajectory()

            landmarks_group = h5f['landmarks']
            instance._landmark_bounds = landmarks_group['visibility_bounds'].value
            positions = landmarks_group['positions'].value
            colors = landmarks_group['colors'].value
            landmark_keys = landmarks_group['visibility'].keys()
            landmark_keys.sort(key=lambda key: int(key))
            for lm_key in landmark_keys:
                lm_id = int(lm_key)
                p = positions[lm_id]
                color = colors[lm_id]
                visibility = set(list(landmarks_group['visibility'][lm_key].value))
                lm = Landmark(p, visibility, color=color)
                instance.landmarks.append(lm)

        return instance

    def _update_trajectory(self):
        smooth_rotations = False
        if self._position_data and not self._orientation_data:
            samp = SampledPositionTrajectory(self._position_data)
            self.trajectory = SplinedPositionTrajectory(samp)
        elif self._orientation_data and not self._position_data:
            samp = SampledRotationTrajectory(self._orientation_data)
            self.trajectory = SplinedRotationTrajectory(samp, smoothRotations=smooth_rotations)
        elif self._position_data and self._orientation_data:
            samp = SampledTrajectory(self._position_data, self._orientation_data)
            self.trajectory = SplinedTrajectory(samp, smoothRotations=smooth_rotations)

class DatasetBuilder(object):
    LANDMARK_SOURCES = ('nvm', 'openmvg')
    SOURCES = ('imu', ) + LANDMARK_SOURCES

    def __init__(self):
        self._nvm_model = None
        self._nvm_camera_fps = None
        self._openmvg_data = None
        self._openmvg_camera_fps = None
        self._gyro_data = None
        self._gyro_times = None

        self._orientation_source = None
        self._position_source = None
        self._landmark_source = None

    @property
    def selected_sources(self):
        return {
            'orientation' : self._orientation_source,
            'position' : self._position_source,
            'landmark' : self._landmark_source
        }

    def add_source_nvm(self, nvm, camera_fps=30.0):
        if self._nvm_model is None:
            self._nvm_model = nvm
            self._nvm_camera_fps = camera_fps
        else:
            raise DatasetError("Can only add one NVM source")

    def add_source_openmvg(self, sfm_data, camera_fps=30.0):
        if self._openmvg_data is None:
            self._openmvg_data = sfm_data
            self._openmvg_camera_fps = camera_fps
        else:
            raise DatasetError("Can only add one OpenMVG source")

    def add_source_gyro(self, gyro_data, gyro_times):
        n, d = gyro_data.shape
        if not n == len(gyro_times):
            raise DatasetError("Gyro data and timestamps length did not match")
        if not d == 3:
            raise DatasetError("Gyro data must have shape Nx3")

        if self._gyro_data is None:
            self._gyro_data = gyro_data
            self._gyro_times = gyro_times
            dt = float(gyro_times[1] - gyro_times[0])
            if not np.allclose(np.diff(gyro_times), dt):
                raise DatasetError("Gyro samples must be uniformly sampled")
            q = crisp.fastintegrate.integrate_gyro_quaternion_uniform(gyro_data, dt)
            self._gyro_quat = QuaternionArray(q)
        else:
            raise DatasetError("Can not add multiple gyro sources")

    def set_orientation_source(self, source):
        if source in self.SOURCES:
            self._orientation_source = source
        else:
            raise DatasetError("No such source type: {}".format(source))

    def set_position_source(self, source):
        if source in self.SOURCES:
            self._position_source = source
        else:
            raise DatasetError("No such source type: {}".format(source))

    def set_landmark_source(self, source):
        if source in self.LANDMARK_SOURCES:
            self._landmark_source = source
        else:
            raise DatasetError("No such source type: {}".format(source))

    def _can_build(self):
        return self._landmark_source is not None and \
                self._orientation_source is not None and \
                self._position_source is not None

    def _nvm_aligned_imu_orientations(self):
        # Start in first camera
        cameras = sorted(self._nvm_model.cameras, key=lambda c: c.framenumber)
        cam_times = np.array([c.framenumber / self._nvm_camera_fps for c in cameras])
        cam_idx = np.flatnonzero(cam_times >= self._gyro_times[0])[0]
        cam_ref = cameras[cam_idx]
        t_ref = cam_times[cam_idx]

        # Find nearest sample
        return self._sfm_aligned_imu_orientations(cam_ref.orientation.conjugate, t_ref)

    def _openmvg_aligned_imu_orientations(self):
        # Start in first view
        views = sorted(self._openmvg_data.views, key=lambda v: v.framenumber)
        view_times = np.array([v.framenumber / self._openmvg_camera_fps for v in views])
        view_idx = np.flatnonzero(view_times >= self._gyro_times[0])[0]
        view_ref = views[view_idx]
        t_ref = view_times[view_idx]
        q_ref = Quaternion.fromMatrix(view_ref.R.T)
        return self._sfm_aligned_imu_orientations(q_ref, t_ref)

    def _sfm_aligned_imu_orientations(self, sfm_q, t_ref):
        gstart_idx = np.argmin(np.abs(self._gyro_times - t_ref))
        q_initial = np.array([sfm_q.w, sfm_q.x, sfm_q.y, sfm_q.z])
        gyro_part = self._gyro_data[gstart_idx:]
        gyro_part_times = self._gyro_times[gstart_idx:]
        dt = float(gyro_part_times[1] - gyro_part_times[0])
        gyro_part = gyro_part
        q = crisp.fastintegrate.integrate_gyro_quaternion_uniform(gyro_part, dt, initial=q_initial)
        # Conjugate to have rotations behave as expected
        q *= np.array([1, -1, -1, -1]).reshape(1,4)
        return q, gyro_part_times


    def build(self):
        if not self._can_build():
            raise DatasetError("Must select all sources")
        ds = Dataset()
        ss = self.selected_sources

        if ss['landmark'] == 'nvm':
            ds.landmarks_from_nvm(self._nvm_model, self._nvm_camera_fps)
        elif ss['landmark'] == 'openmvg':
            ds.landmarks_from_openmvg(self._openmvg_data, self._openmvg_camera_fps)
        else:
            raise DatasetError("'{}' source can not be used for landmarks!".format(ss['landmark']))

        if ss['orientation'] == 'imu':
            if ss['landmark'] == 'nvm':
                orientations, timestamps = self._nvm_aligned_imu_orientations()
                ds.orientation_from_gyro(orientations, timestamps)
            elif ss['landmark'] == 'openmvg':
                orientations, timestamps = self._openmvg_aligned_imu_orientations()
                ds.orientation_from_gyro(orientations, timestamps)
            else:
                ds.orientation_from_gyro(self._gyro_data, self._gyro_times)
        elif ss['orientation'] == 'nvm':
            ds.orientation_from_nvm(self._nvm_model, camera_fps=self._nvm_camera_fps)
        elif ss['orientation'] == 'openmvg':
            ds.orientation_from_openmvg(self._openmvg_data, camera_fps=self._openmvg_camera_fps)
        else:
            raise DatasetError("'{}' source can not be used for orientations!".format(ss['orientation']))

        if ss['position'] == 'nvm':
            ds.position_from_nvm(self._nvm_model, camera_fps=self._nvm_camera_fps)
        elif ss['position'] == 'openmvg':
            ds.position_from_openmvg(self._openmvg_data, camera_fps=self._openmvg_camera_fps)
        else:
            raise DatasetError("'{}' source can not be used for position!".format(ss['position']))

        return ds

def quaternion_slerp(q0, q1, tau):
    q0_arr = np.array([q0.w, q0.x, q0.y, q0.z])
    q1_arr = np.array([q1.w, q1.x, q1.y, q1.z])
    q_arr = crisp.rotations.slerp(q0_arr, q1_arr, tau)
    return Quaternion(*q_arr)

def quaternion_array_interpolate(qa, qtimes, t):
    i = np.flatnonzero(qtimes > t)[0]
    q0 = qa[i-1]
    q1 = qa[i]
    t0 = qtimes[i-1]
    t1 = qtimes[i]
    tau = np.clip((t - t0) / (t1 - t0), 0, 1)

    return quaternion_slerp(q0, q1, tau)

def resample_quaternion_array(qa, timestamps, resize=None):
    num_samples = resize if resize is not None else len(qa)
    timestamps_new = np.linspace(timestamps[0], timestamps[-1], num_samples)
    new_q = []
    unpack = lambda q: np.array([q.w, q.x, q.y, q.z])
    for t in timestamps_new:
        i = np.flatnonzero(timestamps >= t)[0]
        t1 = timestamps[i]
        if np.isclose(t1, t):
            new_q.append(qa[i])
        else:
            t0 = timestamps[i-1]
            tau = (t - t0) / (t1 - t0)
            q0 = qa[i-1]
            q1 = qa[i]
            qc = crisp.rotations.slerp(unpack(q0), unpack(q1), tau)
            q = Quaternion(qc[0], qc[1], qc[2], qc[3])
            new_q.append(q)
    return QuaternionArray(new_q), timestamps_new

def create_bounds(times):
    bounds = [-float('inf')] #[times[0] - 0.5*(times[1] - times[0])]
    for i in range(1, len(times)):
        ta = times[i-1]
        tb = times[i]
        bounds.append(0.5 * (ta + tb))
    #bounds.append(times[-1] + 0.5*(times[-1] - times[-2]))
    bounds.append(float('inf'))
    return bounds
