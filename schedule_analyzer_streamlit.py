import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st


# -----------------------------
# Store settings
# -----------------------------

STORE_TIERS = {
    "Beaumont 5027": "busy",
    "Yucaipa 1504": "busy",
    "Sierra 6176": "mid",
    "Ontario 5052": "mid",
}

STORE_HOURS = {
    "weekday": (time(8, 30), time(18, 30)),
    "saturday": (time(9, 0), time(17, 0)),
    "sunday": (time(10, 0), time(15, 0)),
}

SERVICE_WINDOWS = {
    "weekday": (time(9, 0), time(18, 0)),
    "saturday": (time(10, 0), time(16, 0)),
    "sunday": (time(10, 0), time(15, 0)),
}

PREFERRED_SHIFTS = [
    (time(8, 15), time(13, 30)),
    (time(8, 30), time(16, 30)),
    (time(10, 30), time(18, 0)),
    (time(11, 30), time(18, 45)),
    (time(13, 30), time(18, 45)),
]

DAY_ORDER = ["WED", "THU", "FRI", "SAT", "SUN", "MON", "TUE"]
DAY_TO_KIND = {
    "WED": "weekday",
    "THU": "weekday",
    "FRI": "weekday",
    "SAT": "saturday",
    "SUN": "sunday",
    "MON": "weekday",
    "TUE": "weekday",
}


@dataclass
class Shift:
    employee: str
    day: str
    start: time
    end: time
    role: str


# -----------------------------
# Helper functions
# -----------------------------

def parse_time(raw: str) -> Optional[time]:
    raw = raw.strip().upper().replace(".", "")
    for fmt in ["%I:%M %p", "%I %p"]:
        try:
            return datetime.strptime(raw, fmt).time()
        except ValueError:
            continue
    return None


def minutes_between(start: time, end: time) -> int:
    start_dt = datetime.combine(datetime.today(), start)
    end_dt = datetime.combine(datetime.today(), end)
    return int((end_dt - start_dt).total_seconds() // 60)


def overlaps(a_start: time, a_end: time, b_start: time, b_end: time) -> bool:
    return a_start < b_end and b_start < a_end


def count_active(shifts: List[Shift], check_time: time) -> int:
    return sum(1 for s in shifts if s.start <= check_time < s.end)


def shift_matches_preferred(start: time, end: time, tolerance_minutes: int = 20) -> bool:
    current_start = datetime.combine(datetime.today(), start)
    current_end = datetime.combine(datetime.today(), end)

    for pref_start, pref_end in PREFERRED_SHIFTS:
        ps = datetime.combine(datetime.today(), pref_start)
        pe = datetime.combine(datetime.today(), pref_end)
        if abs((current_start - ps).total_seconds()) <= tolerance_minutes * 60 and abs((current_end - pe).total_seconds()) <= tolerance_minutes * 60:
            return True
    return False


def build_time_checks(kind: str) -> List[time]:
    open_time, close_time = STORE_HOURS[kind]
    checks = []
    current = datetime.combine(datetime.today(), open_time)
    end = datetime.combine(datetime.today(), close_time)
    while current <= end:
        checks.append(current.time())
        current += timedelta(minutes=30)
    return checks


# -----------------------------
# Schedule parsing
# -----------------------------

def parse_gusto_paste(raw_text: str) -> List[Shift]:
    """
    Best-effort parser for copied Gusto weekly schedules.

    Expected pattern:
    Employee Name
    shift line
    role line
    shift line
    role line

    Because copied tables can be messy, the parser walks line by line and assigns
    shifts to the current employee and the next available day in order.
    """
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    shifts: List[Shift] = []

    current_employee = None
    day_index_by_employee: Dict[str, int] = {}

    time_pattern = re.compile(
        r"(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(\d{1,2}:\d{2}\s*[AP]M)",
        re.IGNORECASE,
    )

    skip_keywords = [
        "Schedule for",
        "America/Los_Angeles",
        "Wed,",
        "May 13",
        "WED",
        "THU",
        "FRI",
        "SAT",
        "SUN",
        "MON",
        "TUE",
    ]

    i = 0
    while i < len(lines):
        line = lines[i]

        if any(keyword in line for keyword in skip_keywords):
            i += 1
            continue

        match = time_pattern.search(line)
        if match and current_employee:
            start = parse_time(match.group(1))
            end = parse_time(match.group(2))
            role = lines[i + 1] if i + 1 < len(lines) and not time_pattern.search(lines[i + 1]) else "Unknown"

            if start and end:
                day_idx = day_index_by_employee.get(current_employee, 0)
                if day_idx < len(DAY_ORDER):
                    shifts.append(
                        Shift(
                            employee=current_employee,
                            day=DAY_ORDER[day_idx],
                            start=start,
                            end=end,
                            role=role,
                        )
                    )
                    day_index_by_employee[current_employee] = day_idx + 1
            i += 2
            continue

        # Treat likely name lines as employee names.
        if not match and len(line.split()) >= 2 and not line.lower().endswith("associate") and "manager" not in line.lower() and "supervisor" not in line.lower():
            current_employee = line
            day_index_by_employee.setdefault(current_employee, 0)

        i += 1

    return shifts


# -----------------------------
# Analysis logic
# -----------------------------

def analyze_day(day: str, shifts: List[Shift], store_tier: str, qualifications: pd.DataFrame) -> Dict:
    kind = DAY_TO_KIND[day]
    daily_shifts = [s for s in shifts if s.day == day]

    target_min = 5 if store_tier == "busy" and kind == "weekday" else 4
    ideal = 6 if store_tier == "busy" and kind == "weekday" else 5

    open_time, close_time = STORE_HOURS[kind]
    service_start, service_end = SERVICE_WINDOWS[kind]

    open_coverage = count_active(daily_shifts, open_time)
    close_check = (datetime.combine(datetime.today(), close_time) - timedelta(minutes=30)).time()
    close_coverage = count_active(daily_shifts, close_check)

    total_people = len(set(s.employee for s in daily_shifts))
    manager_count = sum(1 for s in daily_shifts if "manager" in s.role.lower())
    supervisor_count = sum(1 for s in daily_shifts if "supervisor" in s.role.lower())

    preferred_match_count = sum(1 for s in daily_shifts if shift_matches_preferred(s.start, s.end))
    preferred_match_pct = round((preferred_match_count / len(daily_shifts)) * 100, 1) if daily_shifts else 0

    checks = build_time_checks(kind)
    low_coverage_times = []
    for check in checks:
        active = count_active(daily_shifts, check)
        if active < target_min and open_time <= check < close_time:
            low_coverage_times.append(f"{check.strftime('%I:%M %p').lstrip('0')} ({active})")

    notary_gap_times = []
    live_scan_gap_times = []

    if not qualifications.empty:
        qual_map = qualifications.set_index("Employee").to_dict("index")
        service_checks = [t for t in checks if service_start <= t < service_end]

        for check in service_checks:
            active_employees = [s.employee for s in daily_shifts if s.start <= check < s.end]
            notary_count = sum(1 for e in active_employees if bool(qual_map.get(e, {}).get("Notary", False)))
            live_scan_count = sum(1 for e in active_employees if bool(qual_map.get(e, {}).get("Live Scan", False)))

            if notary_count == 0:
                notary_gap_times.append(check.strftime("%I:%M %p").lstrip("0"))
            if live_scan_count == 0:
                live_scan_gap_times.append(check.strftime("%I:%M %p").lstrip("0"))

    warnings = []
    if total_people < target_min:
        warnings.append(f"Below target headcount: {total_people}/{target_min}")
    if open_coverage < 2:
        warnings.append("Opening coverage may be light")
    if close_coverage < 2:
        warnings.append("Closing coverage may be light")
    if day == "WED" and manager_count > 0:
        warnings.append("Wednesday admin day: do not fully count manager as floor coverage")
    if preferred_match_pct < 50 and daily_shifts:
        warnings.append("Many shifts do not match preferred shift templates")
    if notary_gap_times:
        warnings.append("Notary coverage gaps found")
    if live_scan_gap_times:
        warnings.append("Live Scan coverage gaps found")

    if not warnings and total_people >= target_min:
        grade = "Strong"
    elif total_people >= target_min:
        grade = "Covered, Review"
    else:
        grade = "Needs Attention"

    return {
        "Day": day,
        "Store Tier": store_tier.title(),
        "Total People": total_people,
        "Target Min": target_min,
        "Ideal": ideal,
        "Opening Coverage": open_coverage,
        "Closing Coverage": close_coverage,
        "Managers": manager_count,
        "Supervisors": supervisor_count,
        "Preferred Shift Match %": preferred_match_pct,
        "Grade": grade,
        "Low Coverage Times": ", ".join(low_coverage_times[:12]) if low_coverage_times else "None",
        "Notary Gaps": ", ".join(notary_gap_times[:12]) if notary_gap_times else "None / Not Checked",
        "Live Scan Gaps": ", ".join(live_scan_gap_times[:12]) if live_scan_gap_times else "None / Not Checked",
        "Warnings": "; ".join(warnings) if warnings else "None",
    }


def analyze_schedule(shifts: List[Shift], store_tier: str, qualifications: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([analyze_day(day, shifts, store_tier, qualifications) for day in DAY_ORDER])


# -----------------------------
# Streamlit UI
# -----------------------------

st.set_page_config(page_title="Schedule Analyzer", layout="wide")

st.title("Store Schedule Analyzer")
st.caption("Paste a Gusto weekly schedule and quickly flag coverage, preferred shifts, notary gaps, live scan gaps, and admin day concerns.")

with st.sidebar:
    st.header("Store Setup")
    selected_store = st.selectbox("Store", list(STORE_TIERS.keys()))
    store_tier = STORE_TIERS[selected_store]
    st.info(f"Selected tier: {store_tier.title()}")

    st.header("Employee Qualifications")
    st.write("Update this table before running the analysis.")

    default_qualifications = pd.DataFrame(
        {
            "Employee": ["Anthony Gomez", "Ashley DeProsPo", "Eduardo Ramos", "Jenny Aguilar", "Jordan Taylor", "Lisa Rodriguez", "Cynthia Razo", "Amanda Flores", "Bella Espinoza", "Natalia Aguilar", "Nora Morehouse", "Sienna Gonzalez"],
            "Notary": [False] * 12,
            "Live Scan": [False] * 12,
        }
    )

    qualifications = st.data_editor(
        default_qualifications,
        num_rows="dynamic",
        use_container_width=True,
    )

schedule_text = st.text_area(
    "Paste schedule here",
    height=300,
    placeholder="Paste the copied Gusto schedule text here...",
)

analyze_button = st.button("Analyze Schedule", type="primary")

if analyze_button:
    shifts = parse_gusto_paste(schedule_text)

    if not shifts:
        st.error("No shifts were detected. Try pasting the full text version of the Gusto schedule.")
    else:
        shift_rows = [
            {
                "Employee": s.employee,
                "Day": s.day,
                "Start": s.start.strftime("%I:%M %p"),
                "End": s.end.strftime("%I:%M %p"),
                "Role": s.role,
                "Preferred Shift?": "Yes" if shift_matches_preferred(s.start, s.end) else "No",
            }
            for s in shifts
        ]
        shift_df = pd.DataFrame(shift_rows)
        report_df = analyze_schedule(shifts, store_tier, qualifications)

        st.subheader("Schedule Grade by Day")
        st.dataframe(report_df, use_container_width=True)

        st.subheader("Parsed Shifts")
        st.dataframe(shift_df, use_container_width=True)

        st.subheader("Quick Notes")
        weak_days = report_df[report_df["Grade"] != "Strong"]
        if weak_days.empty:
            st.success("Schedule looks strong overall based on current rules.")
        else:
            for _, row in weak_days.iterrows():
                st.warning(f"{row['Day']}: {row['Warnings']}")

        csv = report_df.to_csv(index=False).encode("utf-8")
        st.download_button(
            "Download Report CSV",
            data=csv,
            file_name=f"{selected_store.replace(' ', '_')}_schedule_analysis.csv",
            mime="text/csv",
        )

else:
    st.info("Paste a schedule above, update qualifications if needed, then click Analyze Schedule.")
