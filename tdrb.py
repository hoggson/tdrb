import requests
import asyncio
from datetime import datetime, timedelta

# === CONFIG ===
CHECK_INTERVAL = 60
REMIND_INTERVAL = 300
DEBUG_MODE = False  # Set to False to disable raw JSON logging

PLAYERS = {
    "PLAYER_ID_HERE": {
        "api_key": "TORN_API_KEY_HERE",
        "webhook": "DISCORD_WEBHOOK_HERE"
    },
}

last_cash_state = {pid: False for pid in PLAYERS}
last_cooldowns = {pid: {"drug": None, "booster": None, "medical": None} for pid in PLAYERS}
remind_flags = {pid: {"cash": False, "drug": False} for pid in PLAYERS}
just_triggered = {pid: {"cash": False, "drug": False} for pid in PLAYERS}
energy_suppressed = {pid: False for pid in PLAYERS}
last_hospital_state = {pid: False for pid in PLAYERS}
last_jail_state = {pid: False for pid in PLAYERS}
last_travel_state = {pid: False for pid in PLAYERS}

def key_matches_pid(key, pid):
    return PLAYERS.get(pid, {}).get("api_key") == key

def get_torn_data(pid, key, selections):
    if selections in ["travel", "states"] and key_matches_pid(key, pid):
        url = f"https://api.torn.com/user/?selections={selections}&key={key}"
    else:
        url = f"https://api.torn.com/user/{pid}?selections={selections}&key={key}"

    if DEBUG_MODE:
        print(f"[DEBUG] Fetching: {url}")
    response = requests.get(url)
    try:
        data = response.json()
        if DEBUG_MODE:
            print(f"[DEBUG] Raw response for {pid} ({selections}): {data}")
    except Exception:
        print(f"[ERROR] {pid}: Failed to parse JSON from Torn API.")
        return {}

    if "error" in data:
        print(f"[ERROR] {pid}: Torn API error {data['error'].get('code')} - {data['error'].get('error')}")
        return {}

    return data

def send_named_webhook(name, url, msg):
    if name != "Unknown":
        requests.post(url, json={"content": msg})
    else:
        print(f"[DEBUG] Suppressed message for Unknown player: {msg}")

def format_eta(seconds):
    eta = datetime.now() + timedelta(seconds=seconds)
    return eta.strftime("%H:%M:%S")

async def monitor():
    while True:
        try:
            for pid, info in PLAYERS.items():
                key = info["api_key"]
                webhook_url = info["webhook"]
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                # --- PRIMARY DATA ---
                data_main = get_torn_data(pid, key, "basic,money,cooldowns,bars")
                name = data_main.get("name", "Unknown")
                tag = f"{name} [{pid}]"

                status = data_main.get("status", {})
                status_state = status.get("state", "Unknown")
                status_until = status.get("until", 0)

                # --- TRAVEL DATA ---
                data_travel = get_torn_data(pid, key, "travel")
                travel = data_travel.get("travel", {})
                traveling_now = travel.get("time_left", 0) > 0

                if traveling_now and not last_travel_state[pid]:
                    dest = travel.get("destination", "Unknown")
                    method = travel.get("method", "Unknown")
                    eta = format_eta(travel.get("time_left", 0))
                    send_named_webhook(name, webhook_url, f"✈️ {tag} boarded a flight via {method} to {dest}. ETA: {eta}")
                elif not traveling_now and last_travel_state[pid]:
                    send_named_webhook(name, webhook_url, f"✅ {tag} has landed in Torn.")
                last_travel_state[pid] = traveling_now

                # --- HOSPITAL ---
                in_hospital = status_state == "Hospital"
                if in_hospital and not last_hospital_state[pid]:
                    eta = format_eta(status_until - int(datetime.now().timestamp()))
                    send_named_webhook(name, webhook_url, f"🏥 {tag} is in hospital. Estimated release: {eta}")
                elif not in_hospital and last_hospital_state[pid]:
                    send_named_webhook(name, webhook_url, f"✅ {tag} has left hospital.")
                last_hospital_state[pid] = in_hospital

                # --- JAIL ---
                in_jail = status_state == "Jail"
                if in_jail and not last_jail_state[pid]:
                    eta = format_eta(status_until - int(datetime.now().timestamp()))
                    send_named_webhook(name, webhook_url, f"🚔 {tag} is in jail. Estimated release: {eta}")
                elif not in_jail and last_jail_state[pid]:
                    send_named_webhook(name, webhook_url, f"✅ {tag} has left jail.")
                last_jail_state[pid] = in_jail

                # --- ENERGY ---
                energy = int(data_main.get("energy", {}).get("current", 0))
                energy_suppressed[pid] = (energy == 1000)

                # --- CASH ---
                cash_amount = float(data_main.get("money_onhand", 0))
                carrying_cash = cash_amount > 0

                # --- COOLDOWNS ---
                cooldowns = data_main.get("cooldowns", {})
                drug_cd = cooldowns.get("drug", 0)
                booster_cd = cooldowns.get("booster", 0)
                medical_cd = cooldowns.get("medical", 0)

                # --- SUPPRESSION LOGIC ---
                drug_suppressed = energy_suppressed[pid] or traveling_now or in_jail
                booster_suppressed = traveling_now or in_hospital or in_jail
                medical_suppressed = traveling_now or in_hospital or in_jail
                cash_suppressed = traveling_now or in_jail

                # --- CASH MESSAGES ---
                if carrying_cash and not last_cash_state[pid] and not cash_suppressed:
                    send_named_webhook(name, webhook_url, f"💰 {tag} is carrying cash!")
                    remind_flags[pid]["cash"] = True
                    just_triggered[pid]["cash"] = True
                elif not carrying_cash and last_cash_state[pid]:
                    remind_flags[pid]["cash"] = False
                    just_triggered[pid]["cash"] = False
                last_cash_state[pid] = carrying_cash

                # --- DRUG COOLDOWN END ---
                if drug_cd == 0 and last_cooldowns[pid]["drug"] != 0:
                    send_named_webhook(name, webhook_url, f"⏰ {tag} drug cooldown ended!")
                    if not drug_suppressed:
                        remind_flags[pid]["drug"] = True
                        just_triggered[pid]["drug"] = True
                    else:
                        remind_flags[pid]["drug"] = False
                        just_triggered[pid]["drug"] = False
                elif drug_cd > 0 and last_cooldowns[pid]["drug"] == 0:
                    remind_flags[pid]["drug"] = False
                    just_triggered[pid]["drug"] = False
                last_cooldowns[pid]["drug"] = drug_cd
                last_cooldowns[pid]["booster"] = booster_cd
                last_cooldowns[pid]["medical"] = medical_cd

                # --- ENERGY DROP TRIGGERS DRUG REMINDER ---
                if drug_cd == 0 and not drug_suppressed and not remind_flags[pid]["drug"]:
                    remind_flags[pid]["drug"] = True
                    just_triggered[pid]["drug"] = True

                # --- REMINDERS ---
                if remind_flags[pid]["cash"] and not just_triggered[pid]["cash"] and not cash_suppressed:
                    send_named_webhook(name, webhook_url, f"💰 Reminder: {tag} is still carrying cash!")
                if remind_flags[pid]["drug"] and not just_triggered[pid]["drug"] and not drug_suppressed:
                    send_named_webhook(name, webhook_url, f"💊 Reminder: {tag} drug cooldown is still ready!")

                just_triggered[pid]["cash"] = False
                just_triggered[pid]["drug"] = False

                # --- SUMMARY LOG ---
                suppressed = []
                if energy_suppressed[pid]:
                    suppressed.append("Energy")
                if drug_suppressed:
                    suppressed.append("Drug")
                if booster_suppressed:
                    suppressed.append("Booster")
                if medical_suppressed:
                    suppressed.append("Medical")
                if cash_suppressed:
                    suppressed.append("Cash")
                suppressed_str = "None" if not suppressed else ", ".join(suppressed)

                print(f"{timestamp} | {tag} | Summary: Energy={energy}, Cash={cash_amount:.0f}, DrugCD={drug_cd}, BoosterCD={booster_cd}, MedicalCD={medical_cd}, Status={status_state}, Hospital={'Yes' if in_hospital else 'No'}, Jail={'Yes' if in_jail else 'No'}, Travel={'Yes' if traveling_now else 'No'}, Suppressed=[{suppressed_str}]")

        except Exception as e:
            print("Error:", e)

        if any(f["cash"] or f["drug"] for f in remind_flags.values()):
            await asyncio.sleep(REMIND_INTERVAL)
        else:
            await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(monitor())
