#!/usr/bin/env python3
import ac
import acsys
import os
import sys
import platform
if platform.architecture()[0] == "64bit":
    sys.path.insert(0, "apps/python/kristex-app/stdlib64")
else:
    sys.path.insert(0, "apps/python/kristex-app/stdlib")
os.environ['PATH'] = os.environ['PATH'] + ";."
import sim_info
import socket
import json
import time
import threading

from fastlane_decoder import getNodesFromFastLane, nodes_to_dicts

APP_NAME = "Kristex UDP Track Sender"
WIDTH = 500
HEIGHT = 300

_prepared_track_dicts = None
_prepare_error = None
_prepare_lock = threading.Lock()
udp_sock = None
UDP_ADDR = ("127.0.0.1", 22566)
NODE_SEND_DELAY = 0
_last_cars_info_time = 0
_last_player_info_time = 0
CARS_TELEMETRY_UDP_INFO_INTERVAL = 0.01
PLAYERS_INFO_UDP_INFO_INTERVAL = 0.01
MAX_UDP_PACKET_SIZE = 1024
send_button = None
dicts = None

class PayloadType:
    TRACK_NODE = "track_node"
    CARS_INFO = "cars_info"
    PLAYERS_INFO = "players_info"

class UdpPayload(object):
    
    def __init__(self, payload_type, node_data):
        self.type = payload_type
        self.data = node_data

    def to_dict(self):
        return {
            "type": self.type,
            "data": self.data
        }

    def to_json_line(self):
        import json
        return json.dumps(self.to_dict(), separators=(",", ":")) + "\n"

# ---------------- AC Plugin Globals ----------------
def acMain(ac_version):
    global appWindow

    appWindow = ac.newApp(APP_NAME)
    ac.setTitle(appWindow, APP_NAME)
    ac.setSize(appWindow, WIDTH, HEIGHT)

    ac.addRenderCallback(appWindow, appGL) 
    _start_background_sender_thread()
    _init_buttons(app_window=appWindow)
    return APP_NAME


def acUpdate(deltaT):
    global _last_cars_info_time, _last_player_info_time
    now = time.time()
    if now - _last_cars_info_time >= CARS_TELEMETRY_UDP_INFO_INTERVAL:
        _last_cars_info_time = now
        send_cars_telemetry_udp_chunked()
    if now - _last_player_info_time >= PLAYERS_INFO_UDP_INFO_INTERVAL:
        _last_player_info_time = now
        send_players_info_udp_chunked()

def appGL(deltaT):
    # no custom GL drawing here
    pass

def acShutdown():
    global udp_sock
    if udp_sock:
        try:
            udp_sock.close()
        except Exception:
            pass
        udp_sock = None

# ---------------- UDP Sockets and Sending ----------------
def init_udp_socket():
    global udp_sock
    if udp_sock is None:
        udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            udp_sock.connect(UDP_ADDR)  # optional
        except Exception:
            pass

def send_udp_payload(payload: UdpPayload):
    global udp_sock
    init_udp_socket()
    line = payload.to_json_line().encode("utf-8")
    try:
        udp_sock.send(line)
    except Exception:
        try:
            udp_sock.sendto(line, UDP_ADDR)
        except Exception as e:
            ac.log("[Kristex] Error sending UDP payload: {}".format(e))

def _start_background_sender_thread():
    t = threading.Thread(target=_prepare_and_send_track_thread, daemon=True)
    t.start()
    ac.log("[Kristex] Background prepare/send thread started.")

def _prepare_and_send_track_thread():
    global _prepared_track_dicts, _prepare_error
    try:
        track_name = ac.getTrackName(0)
        ac.log("[Kristex] track_name: '{}' ...".format(track_name))
        track_config = ac.getTrackConfiguration(0)
        ac.log("[Kristex] track_config: '{}' ...".format(track_config))
        if track_config:
            ai_dir = os.path.join("content", "tracks", track_name, track_config, "ai")
        else:
            ai_dir = os.path.join("content", "tracks", track_name, "ai")


        ai_file = os.path.join(ai_dir, "fast_lane.ai")
        ac.log("[Kristex] Loading fast_lane.ai: {}".format(ai_file))

        nodes = getNodesFromFastLane(ai_file)
        dicts = nodes_to_dicts(nodes, include_walls=False, subsample=5)

        with _prepare_lock:
            _prepared_track_dicts = dicts
            _prepare_error = None

        ac.log("[Kristex] Prepared {} nodes from '{}'.".format(len(dicts), ai_file))

        # Immediately stream once on startup
        _udp_stream_dicts(dicts)

    except Exception as e:
        with _prepare_lock:
            _prepared_track_dicts = None
            _prepare_error = str(e)
        ac.log("[Kristex] Preparing track failed: {}".format(str(e)))


def _udp_stream_dicts(dicts):
    """Stream a list of dicts over UDP, one JSON line per dict."""
    if not dicts:
        ac.log("[Kristex] No dicts to send.")
        return

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # optional small connect to set default destination for send (not required for UDP)
        try:
            sock.connect(UDP_ADDR)
        except Exception:
            # ignore connect errors; we'll still use sendto below if connect fails
            pass

        total = len(dicts)
        ac.log("[Kristex] Streaming nodes over UDP to {}:{} ...".format(str(UDP_ADDR[0]), str(UDP_ADDR[1])))

        for i, d in enumerate(dicts):
            try:
                payload = UdpPayload(PayloadType.TRACK_NODE, d)
                # single-line JSON (no extra whitespace), followed by newline
                line = payload.to_json_line()
                # if socket connected successfully, use send(), else use sendto
                try:
                    sock.send(line.encode("utf-8"))
                except Exception:
                    sock.sendto(line.encode("utf-8"), UDP_ADDR)
            except Exception as send_e:
                ac.log("[Kristex] Error sending node")
            # small delay to avoid saturating local network / receiver
            if NODE_SEND_DELAY > 0:
                time.sleep(NODE_SEND_DELAY)

        ac.log("[Kristex] Finished streaming nodes.")
    except Exception as e:
        ac.log("[Kristex] UDP streaming failed: {}".format(e))
    finally:
        try:
            sock.close()
        except Exception:
            pass

def get_all_cars_telemetry():
    telemetry_list = []
    car_count = ac.getCarsCount()
    
    for car_id in range(car_count):
        try:
            car_name = ac.getCarName(car_id)
            pos = ac.getCarState(car_id, acsys.CS.WorldPosition)
            speed = ac.getCarState(car_id, acsys.CS.SpeedTotal)
            best_lap = ac.getCarState(car_id, acsys.CS.BestLap)
            connected = ac.isConnected(car_id)

            telemetry_list.append({
                "id": car_id,
                "name": car_name,
                "position": {"x": pos[2], "y": pos[1], "z": -pos[0]},
                "speed": {"kmh": speed[0], "mph": speed[1], "ms": speed[2]},
                "connected": connected,
                "best_lap": best_lap
            })
            
        except Exception as e:
            ac.log("[Kristex] Error reading telemetry for car {}: {}".format(car_id, e))
    return telemetry_list

def get_all_players_info():
    # Placeholder function to simulate player info retrieval
    players_info = []
    car_count = ac.getCarsCount()
    
    for car_id in range(car_count):
        try:
            player_best_lap = ac.getCarState(car_id, acsys.CS.BestLap)
            player_current_lap = ac.getCarState(car_id, acsys.CS.LapTime)
            player_last_lap = ac.getCarState(car_id, acsys.CS.LastLap)

            player_name = ac.getDriverName(car_id)
            player_car_name = ac.getCarName(car_id)
            player_in_pit = ac.isCarInPitlane(car_id)
            player_in_box = ac.isCarInPit(car_id)
            player_connected = ac.isConnected(car_id)
            player_leaderborad_pos = ac.getCarLeaderboardPosition(car_id)
            player_realtime_leaderboard_pos = ac.getCarRealTimeLeaderboardPosition(car_id)
            player_tyres = ac.getCarTyreCompound(car_id)
            player_splits = ac.getLastSplits(car_id)
            players_info.append({
                "id": car_id,
                "player_name": player_name,
                "car_name": player_car_name,
                "best_lap": player_best_lap,
                "current_lap": player_current_lap,
                "last_lap": player_last_lap,
                "in_pit": player_in_pit,
                "in_box": player_in_box,
                "is_connected": player_connected,
                "leaderboard_pos": player_leaderborad_pos,
                "realtime_leaderboard_pos": player_realtime_leaderboard_pos,
                "tyre_compound": player_tyres,
                "splits": player_splits
            })
        except Exception as e:
            ac.log("[Kristex] Error reading player info for car {}: {}".format(car_id, e))
    return players_info

def send_cars_telemetry_udp_chunked():
    telemetry_data = get_all_cars_telemetry()
    if not telemetry_data:
        ac.log("[Kristex] No telemetry data to send.")
        return

    chunk = []
    chunk_size = 0

    for car in telemetry_data:
        # Estimate the JSON size for this car
        car_json = json.dumps(car, separators=(",", ":"))
        car_bytes = car_json.encode("utf-8")

        # If adding this car exceeds max packet size, send current chunk
        if chunk_size + len(car_bytes) > MAX_UDP_PACKET_SIZE:
            payload = UdpPayload(PayloadType.CARS_INFO, chunk)
            send_udp_payload(payload)
            chunk = []
            chunk_size = 0

        chunk.append(car)
        chunk_size += len(car_bytes)

    # Send any remaining cars
    if chunk:
        payload = UdpPayload(PayloadType.CARS_INFO, chunk)
        send_udp_payload(payload)

def send_players_info_udp_chunked():
    players_info = get_all_players_info()
    if not players_info:
        ac.log("[Kristex] No players data to send.")
        return

    chunk = []
    chunk_size = 0

    for player in players_info:
        # Estimate the JSON size for this player without double dumping
        player_json = json.dumps(player, separators=(",", ":"))
        player_bytes = player_json.encode("utf-8")

        if chunk_size + len(player_bytes) > MAX_UDP_PACKET_SIZE:
            payload = UdpPayload(PayloadType.PLAYERS_INFO, chunk)
            send_udp_payload(payload)
            chunk = []
            chunk_size = 0

        chunk.append(player)   # <-- keep dict, not json string
        chunk_size += len(player_bytes)

    if chunk:
        payload = UdpPayload(PayloadType.PLAYERS_INFO, chunk)
        send_udp_payload(payload)


# ---------------- UI and Button Handling ----------------
def _init_buttons(app_window):
    global send_button
    send_button = ac.addButton(app_window, "Re-send Track (UDP)")
    ac.setPosition(send_button, 20, HEIGHT - 50)
    ac.setSize(send_button, 220, 30)
    ac.addOnClickedListener(send_button, _on_send_button_clicked)

def _on_send_button_clicked(control, state):
    global _prepared_track_dicts, _prepare_error
    ac.log("[Kristex] Send button clicked.")
    with _prepare_lock:
        dicts = None if _prepared_track_dicts is None else list(_prepared_track_dicts)
        err = _prepare_error

    if err:
        ac.log("[Kristex] Cannot send - prepare error: {}".format(err))
        return

    if not dicts:
        ac.log("[Kristex] No prepared track data to send.")
        return

    # spawn a short-lived thread to stream again (so button handler returns immediately)
    t = threading.Thread(target=_udp_stream_dicts, args=(dicts,), daemon=True)
    t.start()
    ac.log("[Kristex] Started resend thread.")
