from cereal import log
from common.numpy_fast import clip, interp
from selfdrive.controls.lib.pid import PIController
from common.travis_checker import travis

LongCtrlState = log.ControlsState.LongControlState

STOPPING_EGO_SPEED = 0.5
MIN_CAN_SPEED = 0.3  # TODO: parametrize this in car interface
STOPPING_TARGET_SPEED = MIN_CAN_SPEED + 0.01
STARTING_TARGET_SPEED = 0.5
BRAKE_THRESHOLD_TO_PID = 0.2

STOPPING_BRAKE_RATE = 0.2  # brake_travel/s while trying to stop
STARTING_BRAKE_RATE = 0.8  # brake_travel/s while releasing on restart
BRAKE_STOPPING_TARGET = 0.75  # apply at least this amount of brake to maintain the vehicle stationary

_MAX_SPEED_ERROR_BP = [0., 30.]  # speed breakpoints
_MAX_SPEED_ERROR_V = [1.5, .8]  # max positive v_pid error VS actual speed; this avoids controls windup due to slow pedal resp

RATE = 100.0


def long_control_state_trans(active, long_control_state, v_ego, v_target, v_pid,
                             output_gb, brake_pressed, cruise_standstill):
  """Update longitudinal control state machine"""
  stopping_condition = (v_ego < 2.0 and cruise_standstill) or \
                       (v_ego < STOPPING_EGO_SPEED and \
                        ((v_pid < STOPPING_TARGET_SPEED and v_target < STOPPING_TARGET_SPEED) or
                        brake_pressed))

  starting_condition = v_target > STARTING_TARGET_SPEED and not cruise_standstill

  if not active:
    long_control_state = LongCtrlState.off

  else:
    if long_control_state == LongCtrlState.off:
      if active:
        long_control_state = LongCtrlState.pid

    elif long_control_state == LongCtrlState.pid:
      if stopping_condition:
        long_control_state = LongCtrlState.stopping

    elif long_control_state == LongCtrlState.stopping:
      if starting_condition:
        long_control_state = LongCtrlState.starting

    elif long_control_state == LongCtrlState.starting:
      if stopping_condition:
        long_control_state = LongCtrlState.stopping
      elif output_gb >= -BRAKE_THRESHOLD_TO_PID:
        long_control_state = LongCtrlState.pid

  return long_control_state


class LongControl():
  def __init__(self, CP, compute_gb):
    self.long_control_state = LongCtrlState.off  # initialized to off
    self.pid = PIController((CP.longitudinalTuning.kpBP, CP.longitudinalTuning.kpV),
                            (CP.longitudinalTuning.kiBP, CP.longitudinalTuning.kiV),
                            rate=RATE,
                            sat_limit=0.8,
                            convert=compute_gb)
    self.v_pid = 0.0
    self.lastdecelForTurn = False
    #self.had_lead = False
    self.last_output_gb = 0.0

    self.gas_pressed = False
    self.lead_data = {'v_rel': None, 'a_lead': None, 'x_lead': None, 'status': False}
    self.mpc_TR = 1.8
    self.blinker_status = False
    self.plan = None

  def reset(self, v_pid):
    """Reset PID controller and change setpoint"""
    self.pid.reset()
    self.v_pid = v_pid

  def handle_params(self, params):
    CS = params['car_state']
    self.blinker_status = CS.leftBlinker or CS.rightBlinker
    self.gas_pressed = CS.gasPressed
    self.lead_data['v_rel'] = params['lead_one'].vRel
    self.lead_data['a_lead'] = params['lead_one'].aLeadK
    self.lead_data['x_lead'] = params['lead_one'].dRel
    self.lead_data['status'] = params['plan'].hasLead  # this fixes radarstate always reporting a lead, thanks to arne
    self.plan = params['plan']
    self.mpc_TR = params['mpc_TR']

  def update(self, active, v_ego, brake_pressed, standstill, cruise_standstill, v_cruise, v_target, v_target_future, a_target, CP, extra_params):
    """Update longitudinal control. This updates the state machine and runs a PID loop"""
    # Actuation limits
    if not travis:
      self.handle_params(extra_params)
      gas_max = None
    else:
      gas_max = interp(v_ego, CP.gasMaxBP, CP.gasMaxV)
    brake_max = interp(v_ego, CP.brakeMaxBP, CP.brakeMaxV)

    # Update state machine
    output_gb = self.last_output_gb
    self.long_control_state = long_control_state_trans(active, self.long_control_state, v_ego,
                                                       v_target_future, self.v_pid, output_gb,
                                                       brake_pressed, cruise_standstill)

    v_ego_pid = max(v_ego, MIN_CAN_SPEED)  # Without this we get jumps, CAN bus reports 0 when speed < 0.3

    if self.long_control_state == LongCtrlState.off or (brake_pressed or self.gas_pressed and not travis):
      self.v_pid = v_ego_pid
      self.pid.reset()
      output_gb = 0.

    # tracking objects and driving
    elif self.long_control_state == LongCtrlState.pid:
      self.v_pid = v_target
      self.pid.pos_limit = gas_max
      self.pid.neg_limit = - brake_max

      # Toyota starts braking more when it thinks you want to stop
      # Freeze the integrator so we don't accelerate to compensate, and don't allow positive acceleration
      prevent_overshoot = not CP.stoppingControl and v_ego < 1.5 and v_target_future < 0.7
      deadzone = interp(v_ego_pid, CP.longitudinalTuning.deadzoneBP, CP.longitudinalTuning.deadzoneV)
      #if not self.had_lead and has_lead:
      #  if enableGasInterceptor:
      #    self.pid._k_p = ([0., 5., 35.], [1.2, 0.8, 0.5])
      #    self.pid._k_i = ([0., 35.], [0.18, 0.12])
      #  else:
      #    self.pid._k_p = ([0., 5., 35.], [3.6, 2.4, 1.5])
      #    self.pid._k_i = ([0., 35.], [0.54, 0.36])
      #elif self.had_lead and not has_lead:
      #  self.pid._k_p = (CP.longitudinalTuning.kpBP, CP.longitudinalTuning.kpV)
      #  self.pid._k_i = (CP.longitudinalTuning.kiBP, CP.longitudinalTuning.kiV)
      #self.had_lead = has_lead
      if self.plan.longitudinalPlanSource == 'cruise':
        if self.plan.decelForTurn and not self.lastdecelForTurn:
          self.lastdecelForTurn = True
          self.pid._k_p = (CP.longitudinalTuning.kpBP, [x * 0 for x in CP.longitudinalTuning.kpV])
          self.pid._k_i = (CP.longitudinalTuning.kiBP, [x * 0 for x in CP.longitudinalTuning.kiV])
          self.pid.i = 0.0
          self.pid.k_f=1.0
        if self.lastdecelForTurn and not self.plan.decelForTurn:
          self.lastdecelForTurn = False
          self.pid._k_p = (CP.longitudinalTuning.kpBP, CP.longitudinalTuning.kpV)
          self.pid._k_i = (CP.longitudinalTuning.kiBP, CP.longitudinalTuning.kiV)
          self.pid.k_f=1.0
      else:
        self.lastdecelForTurn = False
        self.pid._k_p = (CP.longitudinalTuning.kpBP, [x * 1 for x in CP.longitudinalTuning.kpV])
        self.pid._k_i = (CP.longitudinalTuning.kiBP, [x * 1 for x in CP.longitudinalTuning.kiV])
        self.pid.k_f=1.0
      
      output_gb = self.pid.update(self.v_pid, v_ego_pid, speed=v_ego_pid, deadzone=deadzone, feedforward=a_target, freeze_integrator=prevent_overshoot)

      if prevent_overshoot:
        output_gb = min(output_gb, 0.0)

    # Intention is to stop, switch to a different brake control until we stop
    elif self.long_control_state == LongCtrlState.stopping:
      # Keep applying brakes until the car is stopped
      factor = 1
      if self.lead_data['status']:
        factor = interp(self.lead_data['x_lead'], [0.0,1.0,3.0,5.0,6.0,7.0,8.0,9.0], [10.0,5.0,1.8,0.7,0.5,0.1,0.0,-0.1])
      if not standstill or output_gb > -BRAKE_STOPPING_TARGET:
        output_gb -= STOPPING_BRAKE_RATE / RATE * factor
      output_gb = clip(output_gb, -brake_max, gas_max)

      self.v_pid = v_ego
      self.pid.reset()

    # Intention is to move again, release brake fast before handing control to PID
    elif self.long_control_state == LongCtrlState.starting:
      factor = 1
      if self.lead_data['status']:
        factor = interp(self.lead_data['x_lead'], [0.0,2.0,4.0], [0.0,0.5,1.0])
      if output_gb < -0.2:
        output_gb += STARTING_BRAKE_RATE / RATE * factor
      self.v_pid = v_ego
      self.pid.reset()

    self.last_output_gb = output_gb
    final_gas = clip(output_gb, 0., gas_max)
    final_brake = -clip(output_gb, -brake_max, 0.)

    return final_gas, final_brake
