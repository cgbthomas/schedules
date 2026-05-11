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

    This version handles two paste styles:
    1. Tab/table paste from Gusto, where each employee row has 7 day columns.
    2. Plain line-by-line paste, where employee names and shift blocks appear vertically.

    The tab/table version is preferred because it preserves blank days.
    """
    shifts: List[Shift] = []
    time_pattern = re.compile(
        r"(\d{1,2}:\d{2}\s*[AP]M)\s*-\s*(\d{1,2}:\d{2}\s*[AP]M)",
        re.IGNORECASE,
    )

    role_pattern = re.compile(r"(Center Manager|Shift Supervisor|Sales Associate|Notary|Manager)", re.IGNORECASE)

    def clean_cell(cell: str) -> str:
        return re.sub(r"\s+", " ", cell.strip())

    def parse_cell(employee: str, day: str, cell: str) -> None:
        cell = clean_cell(cell)
        if not cell:
            return

        matches = list(time_pattern.finditer(cell))
        if not matches:
            return

        for idx, match in enumerate(matches):
            start = parse_time(match.group(1))
            end = parse_time(match.group(2))
            if not start or not end:
                continue

            role_search_area = cell[match.end():]
            if idx + 1 < len(matches):
                role_search_area = cell[match.end():matches[idx + 1].start()]

            role_match = role_pattern.search(role_search_area)
            role = role_match.group(1).title() if role_match else "Unknown"

            shifts.append(
                Shift(
                    employee=employee,
                    day=day,
                    start=start,
                    end=end,
                    role=role,
                )
            )

    # Preferred parser: tab/table paste from Gusto.
    raw_lines = [line.rstrip("\n") for line in raw_text.splitlines() if line.strip()]
    table_lines = [line for line in raw_lines if "	" in line]

    if table_lines:
        for line in table_lines:
            parts = [p.strip() for p in line.split("	")]
            joined = " ".join(parts)

            if any(x in joined for x in ["Schedule for", "America/Los_Angeles", "WED", "THU", "FRI", "SAT", "SUN", "MON", "TUE", "May 13"]):
                continue

            if len(parts) >= 2 and len(parts[0].split()) >= 2:
                employee = parts[0]
                day_cells = parts[1:8]

                while len(day_cells) < 7:
                    day_cells.append("")

                for day, cell in zip(DAY_ORDER, day_cells):
                    parse_cell(employee, day, cell)

        if shifts:
            return shifts

    # Backup parser: plain line-by-line paste.
    # This cannot perfectly preserve blank days if Gusto removes tabs.
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    current_employee = None
    day_index_by_employee: Dict[str, int] = {}

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

        lower_line = line.lower()
        if not match and len(line.split()) >= 2 and not lower_line.endswith("associate") and "manager" not in lower_line and "supervisor" not in lower_line:
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

st.set_page_config(
    page_title="Schedule Analyzer",
    page_icon="📅",
    layout="wide",
)

st.markdown(
    """
    <style>
    .main {
        background-color: #f7f9fc;
    }
    .block-container {
        padding-top: 2rem;
        padding-bottom: 2rem;
        max-width: 1400px;
    }
    .hero-card {
        background: linear-gradient(135deg, #0f172a 0%, #1e3a8a 55%, #2563eb 100%);
        color: white;
        padding: 28px 32px;
        border-radius: 22px;
        margin-bottom: 22px;
        box-shadow: 0 12px 35px rgba(15, 23, 42, 0.18);
    }
    .hero-title {
        font-size: 34px;
        font-weight: 800;
        margin-bottom: 6px;
    }
    .hero-subtitle {
        font-size: 16px;
        color: #dbeafe;
        margin-bottom: 0px;
    }
    .metric-card {
        background: white;
        padding: 18px 20px;
        border-radius: 18px;
        border: 1px solid #e5e7eb;
        box-shadow: 0 8px 20px rgba(15, 23, 42, 0.06);
    }
    .section-card {
        background: white;
        padding: 22px;
        border-radius: 20px;
        border: 1px solid #e5e7eb;
        box-shadow: 0 8px 20px rgba(15, 23, 42, 0.05);
        margin-bottom: 18px;
    }
    .small-muted {
        color: #64748b;
        font-size: 14px;
    }
    .good-pill {
        background-color: #dcfce7;
        color: #166534;
        padding: 6px 12px;
        border-radius: 999px;
        font-weight: 700;
        font-size: 13px;
    }
    .warn-pill {
        background-color: #fef3c7;
        color: #92400e;
        padding: 6px 12px;
        border-radius: 999px;
        font-weight: 700;
        font-size: 13px;
    }
    .bad-pill {
        background-color: #fee2e2;
        color: #991b1b;
        padding: 6px 12px;
        border-radius: 999px;
        font-weight: 700;
        font-size: 13px;
    }
    div[data-testid="stMetric"] {
        background: white;
        padding: 16px;
        border-radius: 18px;
        border: 1px solid #e5e7eb;
        box-shadow: 0 8px 20px rgba(15, 23, 42, 0.05);
    }
    div[data-testid="stTextArea"] textarea {
        border-radius: 16px;
    }
    .stButton > button {
        border-radius: 999px;
        padding: 0.7rem 1.4rem;
        font-weight: 700;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="hero-card">
        <div class="hero-title">Store Schedule Analyzer</div>
        <p class="hero-subtitle">
            Paste a Gusto weekly schedule and quickly review staffing coverage, preferred shift structure,
            notary/live scan gaps, closing strength, and admin day concerns.
        </p>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.markdown("### Store Setup")
    selected_store = st.selectbox("Select Store", list(STORE_TIERS.keys()))
    store_tier = STORE_TIERS[selected_store]

    if store_tier == "busy":
        st.success("Busy Store Standard: higher staffing target")
    else:
        st.info("Mid-Volume Store Standard")

    st.markdown("---")
    st.markdown("### Coverage Rules")
    st.caption("Weekdays: 8:30 AM–6:30 PM")
    st.caption("Saturday: 9:00 AM–5:00 PM")
    st.caption("Sunday: 10:00 AM–3:00 PM")
    st.caption("Preferred weekday service coverage: 9 AM–6 PM")

    st.markdown("---")
    st.markdown("### Employee Qualifications")
    st.caption("Check notary/live scan boxes before analyzing.")

    default_qualifications = pd.DataFrame(
        {
            "Employee": [
                "Anthony Gomez", "Ashley DeProsPo", "Eduardo Ramos", "Jenny Aguilar", "Jordan Taylor",
                "Lisa Rodriguez", "Cynthia Razo", "Amanda Flores", "Bella Espinoza", "Natalia Aguilar",
                "Nora Morehouse", "Sienna Gonzalez"
            ],
            "Notary": [False] * 12,
            "Live Scan": [False] * 12,
        }
    )

    qualifications = st.data_editor(
        default_qualifications,
        num_rows="dynamic",
        use_container_width=True,
        hide_index=True,
    )

left_col, right_col = st.columns([1.4, 0.8], gap="large")

with left_col:
    st.markdown("<div class='section-card'>", unsafe_allow_html=True)
    st.subheader("Paste Schedule")
    st.caption("Tip: Copy the full Gusto schedule table when possible so blank days stay in place.")
    schedule_text = st.text_area(
        "Gusto schedule text",
        height=320,
        placeholder="Paste the copied Gusto schedule text here...",
        label_visibility="collapsed",
    )
    analyze_button = st.button("Analyze Schedule", type="primary", use_container_width=True)
    st.markdown("</div>", unsafe_allow_html=True)

with right_col:
    st.markdown("<div class='section-card'>", unsafe_allow_html=True)
    st.subheader("Preferred Shift Templates")
    st.markdown(
        """
        - **8:15 AM – 1:30 PM**
        - **8:30 AM – 4:30 PM**
        - **10:30 AM – 6:00 PM**
        - **11:30 AM – 6:45 PM**
        - **1:30 PM – 6:45 PM**
        """
    )
    st.markdown("<p class='small-muted'>The app allows a small time tolerance so close matches are not over-flagged.</p>", unsafe_allow_html=True)
    st.markdown("</div>", unsafe_allow_html=True)


def grade_badge(grade: str) -> str:
    if grade == "Strong":
        return "<span class='good-pill'>Strong</span>"
    if grade == "Covered, Review":
        return "<span class='warn-pill'>Covered, Review</span>"
    return "<span class='bad-pill'>Needs Attention</span>"


if analyze_button:
    shifts = parse_gusto_paste(schedule_text)

    if not shifts:
        st.error("No shifts were detected. Try copying the full Gusto table again, then paste it here.")
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

        strong_days = int((report_df["Grade"] == "Strong").sum())
        review_days = int((report_df["Grade"] == "Covered, Review").sum())
        attention_days = int((report_df["Grade"] == "Needs Attention").sum())
        total_shifts = len(shift_df)
        avg_people = round(report_df["Total People"].mean(), 1)

        st.markdown("### Weekly Snapshot")
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Store", selected_store)
        m2.metric("Avg People/Day", avg_people)
        m3.metric("Strong Days", strong_days)
        m4.metric("Review Days", review_days)
        m5.metric("Needs Attention", attention_days)

        st.markdown("### Daily Coverage Cards")
        card_cols = st.columns(7)
        for idx, (_, row) in enumerate(report_df.iterrows()):
            with card_cols[idx]:
                st.markdown("<div class='metric-card'>", unsafe_allow_html=True)
                st.markdown(f"#### {row['Day']}")
                st.markdown(grade_badge(row["Grade"]), unsafe_allow_html=True)
                st.markdown(f"**People:** {row['Total People']} / {row['Target Min']}")
                st.markdown(f"**Open:** {row['Opening Coverage']}")
                st.markdown(f"**Close:** {row['Closing Coverage']}")
                st.markdown(f"**Shift Match:** {row['Preferred Shift Match %']}%")
                st.markdown("</div>", unsafe_allow_html=True)

        tab1, tab2, tab3, tab4 = st.tabs(["Coverage Report", "Action Items", "Parsed Shifts", "Export"])

        with tab1:
            st.markdown("#### Full Daily Report")
            st.dataframe(
                report_df,
                use_container_width=True,
                hide_index=True,
            )

        with tab2:
            st.markdown("#### Items to Review")
            weak_days = report_df[report_df["Grade"] != "Strong"]
            if weak_days.empty:
                st.success("Schedule looks strong overall based on current rules.")
            else:
                for _, row in weak_days.iterrows():
                    st.warning(f"{row['Day']}: {row['Warnings']}")

            st.markdown("#### Service Coverage")
            notary_issues = report_df[report_df["Notary Gaps"] != "None / Not Checked"]
            live_scan_issues = report_df[report_df["Live Scan Gaps"] != "None / Not Checked"]

            if notary_issues.empty and live_scan_issues.empty:
                st.success("No notary or live scan gaps detected based on checked qualifications.")
            else:
                if not notary_issues.empty:
                    st.error("Notary coverage gaps detected. Check the Coverage Report tab for times.")
                if not live_scan_issues.empty:
                    st.error("Live Scan coverage gaps detected. Check the Coverage Report tab for times.")

        with tab3:
            st.markdown("#### Parsed Shifts")
            st.caption("Use this tab to verify the app read the schedule correctly before trusting the report.")
            st.dataframe(
                shift_df,
                use_container_width=True,
                hide_index=True,
            )

        with tab4:
            csv = report_df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "Download Coverage Report CSV",
                data=csv,
                file_name=f"{selected_store.replace(' ', '_')}_schedule_analysis.csv",
                mime="text/csv",
                use_container_width=True,
            )

            shift_csv = shift_df.to_csv(index=False).encode("utf-8")
            st.download_button(
                "Download Parsed Shifts CSV",
                data=shift_csv,
                file_name=f"{selected_store.replace(' ', '_')}_parsed_shifts.csv",
                mime="text/csv",
                use_container_width=True,
            )

else:
    st.markdown(
        """
        <div class="section-card">
            <h3>How to use this</h3>
            <p class="small-muted">
                1. Select the store from the sidebar.<br>
                2. Check which employees are notaries or live scan trained.<br>
                3. Paste the Gusto weekly schedule.<br>
                4. Click Analyze Schedule.<br>
                5. Review the Daily Coverage Cards and Action Items tab.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )
