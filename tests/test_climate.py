import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.climate import (ClimateManager, DEFAULTS, is_summer, occupancy, setpoint, should_apply, target_call,
                         took_effect)
from app.config import Settings
from app.db import DB
from app.locks import LockManager
from app.reservations import normalize
from app.sync import Syncer
from app.web import create_app

TZ = ZoneInfo("America/New_York")
SETTINGS = Settings(hostaway_account_id="1", hostaway_api_key="x", ha_url="http://ha", ha_token="t",
                    db_path=":memory:", is_addon=False)
CFG = dict(DEFAULTS)


def at(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=TZ)


def res(id=1, arrival="2030-06-12", departure="2030-06-15", check_in=16, check_out=10):
    return {"id": id, "listingMapId": 100, "status": "new", "guestName": "Ann Lee", "arrivalDate": arrival,
            "departureDate": departure, "checkInTime": check_in, "checkOutTime": check_out, "doorCode": "4821"}


def norm(**kw):
    return normalize(res(**kw), TZ, 15, 10)


# ---- pure rules ---------------------------------------------------------------

def test_season_with_and_without_year_wrap():
    assert is_summer(date(2030, 5, 1), 5, 9) and is_summer(date(2030, 9, 30), 5, 9)
    assert not is_summer(date(2030, 4, 30), 5, 9) and not is_summer(date(2030, 10, 1), 5, 9)
    assert is_summer(date(2030, 12, 1), 11, 3) and is_summer(date(2030, 2, 1), 11, 3)
    assert not is_summer(date(2030, 6, 1), 11, 3)


def test_occupied_three_hours_before_check_in_and_vacant_three_hours_after_check_out():
    r = [norm()]  # in 16:00 on the 12th, out 10:00 on the 15th
    assert occupancy(r, at("2030-06-12T12:59"), 3, 3) == "vacant"
    assert occupancy(r, at("2030-06-12T13:00"), 3, 3) == "occupied"
    assert occupancy(r, at("2030-06-15T12:59"), 3, 3) == "occupied"
    assert occupancy(r, at("2030-06-15T13:00"), 3, 3) == "vacant"
    assert occupancy([], at("2030-06-15T13:00"), 3, 3) == "vacant"


def test_same_day_turnover_stays_occupied_with_no_vacant_gap():
    # guest leaves at 10, next guest arrives at 8 PM: occupied from the first checkout on, not vacant at 2 PM
    r = [norm(), norm(id=2, arrival="2030-06-15", departure="2030-06-18", check_in=20)]
    assert occupancy(r, at("2030-06-15T10:30"), 3, 3) == "occupied"
    assert occupancy(r, at("2030-06-15T14:00"), 3, 3) == "occupied"
    assert occupancy(r, at("2030-06-15T21:00"), 3, 3) == "occupied"
    assert occupancy(r, at("2030-06-18T13:00"), 3, 3) == "vacant"


def test_cancelled_bookings_do_not_count():
    r = [normalize({**res(), "status": "cancelled"}, TZ, 15, 10)]
    assert occupancy(r, at("2030-06-13T12:00"), 3, 3) == "vacant"


def test_setpoints_follow_client_numbers():
    assert (setpoint("occupied", True, CFG), setpoint("occupied", False, CFG)) == (72, 66)
    assert (setpoint("vacant", True, CFG), setpoint("vacant", False, CFG)) == (78, 55)


def test_should_apply():
    now = at("2030-06-12T12:00")
    assert should_apply("vacant", None, None, now, 11)  # first time
    assert should_apply("occupied", "vacant", now, now, 11)  # changed
    assert not should_apply("occupied", "occupied", at("2030-06-11T12:00"), now, 11)  # guests keep their own tweaks
    assert not should_apply("vacant", "vacant", at("2030-06-12T08:00"), now, 11)  # already done today
    assert should_apply("vacant", "vacant", at("2030-06-11T12:00"), now, 11)  # next day: cleaners may have touched it
    assert not should_apply("vacant", "vacant", at("2030-06-11T12:00"), at("2030-06-12T10:59"), 11)  # not yet


def test_range_thermostats_move_the_right_bound():
    heat_cool = {"state": "heat_cool", "target_low": 66, "target_high": 74}
    assert target_call(heat_cool, 78, True) == {"high": 78.0, "low": 66.0}
    assert target_call(heat_cool, 55, False) == {"low": 55.0, "high": 74.0}
    assert target_call({"state": "cool", "target_temp": 72}, 78, True) == {"temperature": 78.0}
    assert took_effect({"temperature": 78.0}, {"target_temp": 78})
    assert took_effect({"temperature": 78.0}, {"target_temp": 77.5})
    assert not took_effect({"temperature": 78.0}, {"target_temp": 72})
    assert not took_effect({"temperature": 78.0}, {"target_temp": None})


# ---- manager ------------------------------------------------------------------

class FakeHA:
    def __init__(self):
        self.climate = {"climate.maple": {"entity_id": "climate.maple", "name": "Maple thermostat", "state": "cool",
                                          "current_temp": 74, "target_temp": 72, "target_low": None,
                                          "target_high": None, "hvac_action": "cooling", "fan_mode": "auto"}}
        self.sets = []
        self.modes = []
        self.mode_fails = False
        self.mode_ranges = {}  # mode -> (min, max) the thermostat accepts once it is in that mode
        self.ignore_sets = False
        self.batteries = {}
        self.notes = []

    async def climate_states(self):
        return [dict(v) for v in self.climate.values()]

    async def set_temperature(self, entity_id, *, temperature=None, low=None, high=None):
        self.sets.append((entity_id, temperature, low, high))
        if self.ignore_sets:
            return
        state = self.climate[entity_id]
        if temperature is not None:
            state["target_temp"] = temperature
        if low is not None:
            state["target_low"] = low
        if high is not None:
            state["target_high"] = high

    async def set_hvac_mode(self, entity_id, mode):
        self.modes.append((entity_id, mode))
        if self.mode_fails:
            raise RuntimeError("mode refused")
        state = self.climate[entity_id]
        state["state"] = mode
        if mode in self.mode_ranges:
            state["min_temp"], state["max_temp"] = self.mode_ranges[mode]

    async def lock_batteries(self):
        return dict(self.batteries)

    async def notify(self, title, message, service="", services=None, notification_id=""):
        self.notes.append((title, message))


@pytest.fixture
def cm():
    db = DB(":memory:")
    s = Syncer(db, SETTINGS, object(), FakeHA())
    pid = db.execute("INSERT INTO properties(hostaway_listing_id, hostaway_name, name, thermostat_automation) "
                     "VALUES(100, 'Maple 1', 'Maple 1', 1)")
    db.execute("INSERT INTO thermostats(entity_id, name, property_id, match_source) "
               "VALUES('climate.maple', 'Maple thermostat', ?, 'manual')", (pid,))
    db.execute("INSERT INTO locks(entity_id, name, state, property_id, match_source) "
               "VALUES('lock.maple', 'Maple front', 'locked', ?, 'manual')", (pid,))
    s.upsert_reservation(res())
    m = ClimateManager(s, LockManager(s).alert)
    m.verify_delay = 0
    return m


def tick(m, when):
    m.s.now = lambda: when
    return asyncio.run(m.tick())


def row(m):
    return m.db.one("SELECT * FROM thermostats")


def test_full_stay_cycle_in_summer(cm):
    assert tick(cm, at("2030-06-10T12:00")) == 1
    assert cm.ha.sets == [("climate.maple", 78.0, None, None)]  # vacant, summer
    assert row(cm)["last_mode"] == "vacant"

    assert tick(cm, at("2030-06-10T12:05")) == 0  # nothing to do
    tick(cm, at("2030-06-11T10:00"))
    assert len(cm.ha.sets) == 1  # next day but before the reset hour
    tick(cm, at("2030-06-11T11:05"))
    assert len(cm.ha.sets) == 2  # day two: set to vacant again

    tick(cm, at("2030-06-12T13:00"))
    assert cm.ha.sets[-1] == ("climate.maple", 72.0, None, None)  # occupied three hours before check-in
    tick(cm, at("2030-06-13T12:00"))
    tick(cm, at("2030-06-14T12:00"))
    assert len(cm.ha.sets) == 3  # guest's own tweaks are left alone during the stay

    tick(cm, at("2030-06-15T12:00"))
    assert len(cm.ha.sets) == 3  # still within three hours after check-out
    tick(cm, at("2030-06-15T13:00"))
    assert cm.ha.sets[-1] == ("climate.maple", 78.0, None, None)
    assert [e["kind"] for e in cm.db.query("SELECT kind FROM events")].count("thermostat.set") == 4


def test_winter_numbers(cm):
    cm.db.execute("DELETE FROM reservations")
    tick(cm, at("2030-12-10T12:00"))
    assert cm.ha.sets[-1][1] == 55.0
    cm.s.upsert_reservation(res(arrival="2030-12-12", departure="2030-12-15"))
    tick(cm, at("2030-12-12T13:00"))
    assert cm.ha.sets[-1][1] == 66.0


def test_edited_numbers_are_used(cm):
    cm.db.set_setting("vac_summer", "80")
    tick(cm, at("2030-06-10T12:00"))
    assert cm.ha.sets[-1][1] == 80.0


def test_target_outside_the_thermostats_own_limits_is_clamped_and_logged(cm):
    cm.ha.climate["climate.maple"].update(min_temp=68, max_temp=74)
    tick(cm, at("2030-06-10T12:00"))  # vacant in summer wants 78, the device stops at 74
    assert cm.ha.sets == [("climate.maple", 74.0, None, None)]
    assert row(cm)["last_mode"] == "vacant" and row(cm)["fail_count"] == 0
    note = cm.db.one("SELECT message FROM events WHERE kind = 'thermostat.limited'")["message"]
    assert "wanted 78" in note and "68-74" in note


def test_range_thermostat_in_summer_moves_cooling_bound_only(cm):
    cm.ha.mode_fails = True  # cannot be switched to cool, so the two-setpoint path is what is left
    cm.ha.climate["climate.maple"].update(state="heat_cool", target_temp=None, target_low=66, target_high=74)
    tick(cm, at("2030-06-10T12:00"))
    assert cm.ha.sets == [("climate.maple", None, 66.0, 78.0)]
    assert cm.db.one("SELECT 1 FROM events WHERE kind = 'thermostat.mode_failed'")


def test_summer_switches_to_cool_first_and_uses_the_cooling_range(cm):
    cm.ha.climate["climate.maple"].update(state="heat_cool", target_temp=None, target_low=66, target_high=74,
                                          min_temp=69, max_temp=74)
    cm.ha.mode_ranges["cool"] = (68, 90)  # in cool the thermostat accepts up to 90
    tick(cm, at("2030-06-10T12:00"))  # vacant, summer: 78
    assert cm.ha.modes == [("climate.maple", "cool")]
    assert cm.ha.sets == [("climate.maple", 78.0, None, None)]  # not cut to 74
    assert cm.db.one("SELECT 1 FROM events WHERE kind = 'thermostat.mode'")
    assert not cm.db.one("SELECT 1 FROM events WHERE kind = 'thermostat.limited'")


def test_winter_switches_to_heat(cm):
    cm.db.execute("DELETE FROM reservations")
    tick(cm, at("2030-12-10T12:00"))
    assert cm.ha.modes == [("climate.maple", "heat")]


def test_a_thermostat_already_in_the_right_mode_is_not_switched(cm):
    tick(cm, at("2030-06-10T12:00"))  # it is already in cool
    assert cm.ha.modes == []


def test_switching_on_mid_stay_leaves_the_guest_alone(cm):
    assert tick(cm, at("2030-06-13T12:00")) == 0
    assert cm.ha.sets == []
    assert row(cm)["last_mode"] == "occupied"
    assert cm.db.one("SELECT 1 FROM events WHERE kind = 'thermostat.adopted'")
    tick(cm, at("2030-06-15T13:00"))  # but checkout still sets it vacant
    assert cm.ha.sets[-1][1] == 78.0


def test_homes_without_automation_or_pairing_are_never_touched(cm):
    cm.db.execute("UPDATE properties SET thermostat_automation = 0")
    tick(cm, at("2030-06-10T12:00"))
    cm.db.execute("UPDATE properties SET thermostat_automation = 1")
    cm.db.execute("UPDATE thermostats SET property_id = NULL")
    tick(cm, at("2030-06-10T12:05"))
    assert cm.ha.sets == []


def test_a_failed_change_waits_before_it_is_tried_again(cm):
    cm.ha.ignore_sets = True
    for minute in (0, 5, 10, 55):
        tick(cm, at(f"2030-06-10T12:{minute:02d}"))
    assert len(cm.ha.sets) == 1  # one try, then it waits (default: an hour)
    tick(cm, at("2030-06-10T13:01"))
    assert len(cm.ha.sets) == 2


def test_a_thermostat_that_ignores_the_command_is_retried_and_then_alerts(cm):
    cm.db.set_setting("therm_retry_minutes", "5")
    cm.ha.ignore_sets = True
    for minute in (0, 5, 10, 15):
        tick(cm, at(f"2030-06-10T12:{minute:02d}"))
    assert len(cm.ha.sets) == 4  # retried every tick
    assert row(cm)["last_mode"] is None and row(cm)["fail_count"] == 4
    alerts = [t for t, _ in cm.ha.notes if t.startswith("Thermostat not responding")]
    assert len(alerts) == 1  # once a day, not every retry
    cm.ha.ignore_sets = False
    tick(cm, at("2030-06-10T12:20"))
    assert row(cm)["last_mode"] == "vacant" and row(cm)["fail_count"] == 0


def test_offline_thermostat_alerts_once_after_the_limit_and_is_not_commanded(cm):
    cm.ha.climate["climate.maple"]["state"] = "unavailable"
    tick(cm, at("2030-06-10T12:00"))
    assert row(cm)["offline_since"] and cm.ha.sets == []
    tick(cm, at("2030-06-10T12:20"))
    assert not cm.ha.notes  # 20 minutes: under the 30 minute limit
    tick(cm, at("2030-06-10T12:31"))
    tick(cm, at("2030-06-10T13:00"))
    assert [t for t, _ in cm.ha.notes] == ["Thermostat offline: Maple 1"]
    assert cm.ha.sets == []

    cm.ha.climate["climate.maple"]["state"] = "cool"  # comes back: gets its setting
    tick(cm, at("2030-06-10T13:05"))
    assert row(cm)["offline_since"] is None and len(cm.ha.sets) == 1


def test_low_battery_is_stored_and_alerted_once_a_week(cm):
    cm.ha.batteries = {"lock.maple": 12}
    cm.s.now = lambda: at("2030-06-10T12:00")
    assert asyncio.run(cm.check_batteries()) == 1
    assert cm.db.one("SELECT battery FROM locks")["battery"] == 12
    cm.s.now = lambda: at("2030-06-11T12:00")
    asyncio.run(cm.check_batteries())
    assert [t for t, _ in cm.ha.notes] == ["Lock battery low: Maple 1"]
    cm.s.now = lambda: at("2030-06-18T12:00")  # next week: a reminder
    asyncio.run(cm.check_batteries())
    assert len(cm.ha.notes) == 2
    cm.ha.batteries = {"lock.maple": 80}
    assert asyncio.run(cm.check_batteries()) == 0


def test_discovery_adds_and_matches_thermostats(cm):
    cm.ha.climate["climate.cedar"] = {**cm.ha.climate["climate.maple"], "entity_id": "climate.cedar",
                                      "name": "Maple 1 Upstairs Thermostat"}
    asyncio.run(cm.refresh())
    new = cm.db.one("SELECT * FROM thermostats WHERE entity_id = 'climate.cedar'")
    assert new["property_id"] == cm.db.one("SELECT id FROM properties")["id"] and new["match_source"] == "auto"


# ---- dashboard -----------------------------------------------------------------

def client(cm):
    return TestClient(create_app(cm.s), follow_redirects=False)


def test_status_page_shows_temperatures_and_battery(cm):
    cm.db.execute("UPDATE locks SET battery = 12")
    asyncio.run(cm.refresh())
    page = client(cm).get("/")
    assert page.status_code == 200
    assert "74°" in page.text and "72°" in page.text and "cool · cooling · fan auto" in page.text
    assert "battery 12%" in page.text  # flagged as needing attention (limit is 15)


def test_status_page_shows_offline_thermostat_as_a_problem(cm):
    cm.ha.climate["climate.maple"]["state"] = "unavailable"
    asyncio.run(cm.refresh())
    assert "Maple thermostat offline" in client(cm).get("/").text


def _form(cm, **over):
    p = cm.db.one("SELECT * FROM properties")
    t = cm.db.one("SELECT * FROM thermostats")
    data = {f"name_{p['id']}": p["name"], f"was_auto_{p['id']}": "0", f"was_tauto_{p['id']}": "1",
            f"tauto_{p['id']}": "on", f"was_active_{p['id']}": "1", f"active_{p['id']}": "on",
            f"was_thermo_{t['id']}": str(p["id"]), f"thermo_{t['id']}": str(p["id"])}
    data.update(over)
    return data, p, t


def test_properties_page_pairs_and_toggles_thermostats(cm):
    c = client(cm)
    page = c.get("/properties")
    assert page.status_code == 200 and "Maple thermostat" in page.text and "tauto_" in page.text

    data, p, t = _form(cm)
    data.pop(f"tauto_{p['id']}")  # untick
    assert c.post("/properties-save", data=data).status_code == 303
    assert cm.db.one("SELECT thermostat_automation FROM properties")["thermostat_automation"] == 0
    assert cm.db.one("SELECT 1 FROM events WHERE kind = 'tauto.off'")

    data, p, t = _form(cm, **{f"was_tauto_{p['id']}": "0"})  # tick it again
    assert c.post("/properties-save", data=data).status_code == 303
    row_ = cm.db.one("SELECT * FROM properties")
    assert row_["thermostat_automation"] == 1 and cm.db.one("SELECT last_mode FROM thermostats")["last_mode"] is None

    data, p, t = _form(cm, **{f"thermo_{t['id']}": ""})  # unassign
    c.post("/properties-save", data=data)
    assert cm.db.one("SELECT property_id FROM thermostats")["property_id"] is None


def test_thermostat_settings_validated_then_saved(cm):
    c = client(cm)
    good = {k: str(v) for k, v in DEFAULTS.items()}
    assert c.post("/thermostat-settings-save", data={**good, "vac_summer": "abc"}).status_code == 400
    assert c.post("/thermostat-settings-save", data={**good, "summer_start_month": "13"}).status_code == 400
    assert c.post("/thermostat-settings-save", data={**good, "vac_winter": "70"}).status_code == 400  # warmer than occ
    assert c.post("/thermostat-settings-save", data={**good, "vac_summer": "70"}).status_code == 400  # cooler than occ
    assert cm.db.get_setting("vac_summer") is None  # nothing saved by the rejected posts
    assert c.post("/thermostat-settings-save", data={**good, "vac_summer": "80"}).status_code == 303
    assert cm.db.get_int("vac_summer", 0) == 80
    assert "Thermostat automation" in c.get("/setup").text


def test_street_sort_groups_by_street_then_number():
    from app.web import street_key
    names = ["Maple 124b", "Cedar 302", "124 Maple", "Maple 9", "CC6", "Cedar 31"]
    assert sorted(names, key=street_key) == ["CC6", "Cedar 31", "Cedar 302", "Maple 9", "124 Maple", "Maple 124b"]


def test_status_filters_and_sorting(cm):
    c = client(cm)
    cm.db.execute("INSERT INTO properties(hostaway_listing_id, hostaway_name, name) VALUES(200, 'Aspen 5', 'Aspen 5')")
    asyncio.run(cm.refresh())
    everything = c.get("/").text
    assert "Maple 1" in everything and "Aspen 5" in everything
    by_name = c.get("/?sort=name").text
    assert by_name.index("Aspen 5") < by_name.index("Maple 1")
    arriving = c.get("/?show=arriving").text
    assert "No homes match" in arriving  # nobody arrives today in the test data
    assert c.get("/?show=nonsense").status_code == 200  # unknown filters fall back to all
    assert "Next check-out" not in everything and "→" in everything  # stay shows start → end
