import json
import random

random.seed(42)

DEPARTMENTS = [
    "Mechanical Maintenance", "Instrumentation & Control", "Process Safety",
    "Rotating Equipment", "Static Equipment", "Electrical Maintenance",
    "Inspection & NDT", "HSE", "Utilities", "Reliability Engineering",
]

EMPLOYEES = [
    "R. Sharma", "A. Menon", "S. Iyer", "P. Verma", "K. Nair",
    "V. Reddy", "N. Joshi", "T. Pillai", "M. Kulkarni", "D. Rao",
]

APPROVER_TITLES = [
    "Shift In-Charge, Operations", "Deputy Manager, Maintenance",
    "Chief Inspector, Static Equipment", "Manager, Process Safety",
    "Section Head, Rotating Equipment", "Plant Manager",
]

INSTRUCTION_VARIANTS = [
    "Summarize this inspection finding as a formal approval note.",
    "Draft a formal approval note based on the following maintenance finding.",
    "Write an approval note for the following equipment observation.",
    "Convert this field note into a structured approval note.",
    "Prepare a formal approval note documenting the following finding.",
]

# Each tuple: (subject, raw_field_note, findings_summary, recommendation, status)
SCENARIOS = [
    ("Pump P-104 — Vibration Anomaly",
     "operator noted high vibration on pump P-104 during routine rounds on 3rd shift, readings above normal band on outboard bearing",
     "During routine operator rounds, Pump P-104 (outboard bearing) recorded vibration readings above the established alarm threshold. No abnormal noise or leakage was observed at the time of inspection. Bearing temperature remained within normal range.",
     "Schedule a detailed vibration analysis within 48 hours. Continue operation under close monitoring; reduce inspection interval to once per shift until root cause is confirmed.",
     "Approved with condition — enhanced monitoring"),
    ("Pump P-201 — Bearing Replacement Completed",
     "maintenance team replaced drive-end bearing on P-201 after scheduled overhaul, post-replacement run test showed normal vibration and temperature",
     "Scheduled overhaul of Pump P-201 was completed, including replacement of the drive-end bearing. Post-replacement run test confirmed vibration and bearing temperature within normal operating limits.",
     "Return pump to normal service. Log bearing replacement in the equipment history card and schedule next inspection per standard maintenance interval.",
     "Approved"),
    ("Relief Valve RV-312 — Set Pressure Verification",
     "relief valve RV-312 removed for scheduled bench test, set pressure verified at test bench within tolerance",
     "Relief Valve RV-312 was removed from service for scheduled bench testing. The set pressure was verified on the test bench and found within the acceptable tolerance band specified by the equipment datasheet.",
     "Reinstall RV-312 into service. Update the valve test certificate and file with the inspection department records.",
     "Approved"),
    ("Storage Tank T-08 — Corrosion Under Insulation Finding",
     "NDT team found localized corrosion under insulation on tank T-08 shell near the north nozzle during scheduled UT survey",
     "Ultrasonic thickness survey of Storage Tank T-08 identified localized corrosion under insulation near the north nozzle. Measured wall thickness remains above the minimum required thickness but shows a declining trend versus the previous survey.",
     "Schedule insulation removal and detailed visual inspection of the affected area within the current quarter. Include the location in the next corrosion monitoring cycle at reduced interval.",
     "Approved with condition — follow-up inspection required"),
    ("Compressor C-501 — Unusual Noise During Startup",
     "operator reported unusual knocking noise from compressor C-501 during cold startup, noise subsided after unit reached operating temperature",
     "An unusual knocking noise was reported from Compressor C-501 during cold startup. The noise subsided once the unit reached normal operating temperature, and subsequent parameters (discharge pressure, temperature, vibration) were within normal range.",
     "Continue operation with close monitoring during future startups. Raise a maintenance notification to inspect coupling alignment and lubrication system at the next planned shutdown.",
     "Approved with condition — monitor at next startup"),
    ("Heat Exchanger E-22 — Fouling Observation",
     "process engineer observed declining heat transfer performance on exchanger E-22 over the past month, outlet temperature trending lower than design",
     "A gradual decline in heat transfer performance was observed on Heat Exchanger E-22 over the past month, with outlet temperature trending below design values, consistent with progressive tube-side fouling.",
     "Schedule the exchanger for chemical cleaning at the next available process window. Continue monitoring outlet temperature trend weekly until cleaning is performed.",
     "Approved — schedule cleaning"),
    ("Control Valve CV-77 — Erratic Positioner Response",
     "instrumentation technician found control valve CV-77 positioner responding erratically during loop check, output signal unstable",
     "During a routine loop check, the positioner on Control Valve CV-77 exhibited erratic response with an unstable output signal. Manual stroke test confirmed inconsistent valve travel versus commanded signal.",
     "Place the loop in manual control until the positioner is recalibrated or replaced. Raise a work order for positioner inspection within 24 hours.",
     "Approved with condition — manual override until repaired"),
    ("Cooling Tower CT-3 — Fan Motor Vibration",
     "rotating equipment engineer noted elevated vibration on cooling tower CT-3 fan motor during monthly condition monitoring round",
     "Monthly condition monitoring of Cooling Tower CT-3 fan motor recorded vibration levels above the baseline established during commissioning, though still within the equipment's alarm limit.",
     "Increase monitoring frequency to weekly. Trend the vibration data and raise a maintenance notification if the increasing trend continues over the next two readings.",
     "Approved with condition — enhanced monitoring"),
    ("Pipeline Section PL-14 — External Coating Damage",
     "field inspection identified damaged external coating on pipeline section PL-14 near support bracket, base metal partially exposed",
     "Field inspection of Pipeline Section PL-14 identified damaged external protective coating near a support bracket, with partial exposure of the base metal. No active leakage or significant wall loss was observed at the time of inspection.",
     "Schedule coating repair within the current month. Perform a spot ultrasonic thickness check at the exposed location prior to recoating to confirm no accelerated corrosion has occurred.",
     "Approved with condition — repair within 30 days"),
    ("Flare Stack FS-1 — Pilot Flame Interruption",
     "operations reported brief loss of pilot flame on flare stack FS-1 during a weather event, auto-reignition system restored flame within two minutes",
     "A brief interruption of the pilot flame on Flare Stack FS-1 occurred during a severe weather event. The automatic re-ignition system restored the pilot flame within two minutes, and no unignited flaring was observed during the interruption.",
     "Log the event in the flare system history. Verify auto-reignition system functionality during the next scheduled test and review weather-related trip settings.",
     "Approved — no further action required"),
    ("Boiler B-2 — Safety Valve Test Overdue Flag",
     "inspection records indicate the annual safety valve test for boiler B-2 is due within the next two weeks, unit currently in service",
     "A review of inspection records indicates the annual statutory safety valve test for Boiler B-2 is due within the next two weeks. The unit remains in normal service with no reported anomalies.",
     "Schedule the safety valve test with the inspection department before the due date. Prepare a short outage window in coordination with operations to avoid an overdue statutory test.",
     "Approved — schedule test before due date"),
    ("Electrical Panel EP-9 — Thermal Scan Hotspot",
     "electrical maintenance team identified a hotspot during routine thermal imaging scan of panel EP-9, temperature rise above ambient baseline at one breaker connection",
     "Routine thermal imaging of Electrical Panel EP-9 identified a localized hotspot at one breaker connection, with temperature rise above the ambient baseline. No visible damage or discoloration was observed at the connection point.",
     "De-energize and tighten the affected connection at the earliest safe opportunity, ideally within one week. Re-scan the panel after the corrective action to confirm the hotspot is resolved.",
     "Approved with condition — corrective action within one week"),
    ("Welded Joint WJ-45 — NDT Result Review",
     "radiographic testing of welded joint WJ-45 on the new pipe spool completed, film review shows one minor porosity indication within acceptance criteria",
     "Radiographic testing of Welded Joint WJ-45 on the newly fabricated pipe spool was completed. Film review identified one minor porosity indication, which falls within the acceptance criteria of the applicable welding code.",
     "Accept the weld as-is per code acceptance criteria. File the radiographic film and test report with the quality control records for the job.",
     "Approved"),
    ("Effluent Treatment Unit ETU-2 — pH Excursion",
     "environmental monitoring recorded a brief pH excursion outside the permitted range at the ETU-2 outlet, corrected within thirty minutes by dosing adjustment",
     "A brief excursion of outlet pH outside the permitted range was recorded at Effluent Treatment Unit ETU-2. The excursion was corrected within thirty minutes through an adjustment to the chemical dosing rate, and subsequent readings confirmed return to the permitted range.",
     "Log the excursion in the environmental incident register per statutory reporting requirements. Review dosing control logic to reduce the likelihood of recurrence.",
     "Approved — log and review dosing control"),
    ("Nitrogen Purge System N2-6 — Pressure Regulator Failure",
     "instrumentation team found the pressure regulator on nitrogen purge system N2-6 failed to maintain setpoint during a routine functional check, backup regulator engaged automatically",
     "During a routine functional check of Nitrogen Purge System N2-6, the primary pressure regulator failed to maintain the specified setpoint. The backup regulator engaged automatically, and purge pressure remained within safe operating limits throughout.",
     "Replace the failed primary regulator at the earliest opportunity. Verify backup regulator changeover logic during the next scheduled functional test.",
     "Approved with condition — replace primary regulator"),
    ("Crane CR-4 — Load Test Certificate Renewal",
     "annual load test of overhead crane CR-4 completed successfully at rated capacity, all safety interlocks functioning correctly",
     "The annual statutory load test of Overhead Crane CR-4 was completed successfully at rated capacity. All safety interlocks, including overload cutoff and limit switches, were confirmed functioning correctly during the test.",
     "Renew the crane's load test certificate and update the equipment register. Return the crane to normal service.",
     "Approved"),
    ("Instrument Air Compressor IAC-1 — Dew Point Deviation",
     "utilities team observed instrument air dew point trending higher than the specification on IAC-1 dryer over the past week",
     "A gradual increase in instrument air dew point above specification was observed on the IAC-1 dryer system over the past week, though values remain below the level at which instrument malfunction risk becomes significant.",
     "Inspect and service the dryer desiccant bed within the next five working days. Continue daily dew point logging until the trend is corrected.",
     "Approved with condition — service within five days"),
    ("Scaffold Structure SC-19 — Pre-Use Inspection",
     "safety officer completed pre-use inspection of scaffold SC-19 erected for tank T-08 insulation work, structure found stable and properly tagged",
     "Pre-use inspection of Scaffold Structure SC-19, erected to support insulation removal work on Tank T-08, confirmed the structure is stable, correctly tagged, and compliant with the site scaffolding standard.",
     "Approve the scaffold for use. Schedule the next periodic inspection per the standard seven-day interval for scaffolds remaining in place.",
     "Approved"),
    ("Fire Water Pump FWP-2 — Weekly Churn Test",
     "fire water pump FWP-2 weekly churn test completed, pump started automatically on demand and achieved rated discharge pressure within specified time",
     "The weekly churn test of Fire Water Pump FWP-2 was completed. The pump started automatically on demand and achieved rated discharge pressure within the specified startup time, confirming operational readiness.",
     "Log the successful test result in the fire protection system register. No further action required until the next scheduled test.",
     "Approved — no further action required"),
    ("Distillation Column DC-3 — Tray Damage Suspicion",
     "process data analysis suggests possible tray damage in distillation column DC-3 based on abnormal pressure drop pattern over the past two weeks",
     "Analysis of process data over the past two weeks indicates an abnormal pressure drop pattern across Distillation Column DC-3, consistent with possible internal tray damage. Product quality remains within specification at present.",
     "Schedule an internal inspection during the next planned shutdown. In the interim, continue trending the pressure drop pattern and notify process engineering of any further deviation.",
     "Approved with condition — internal inspection at next shutdown"),
]


def build_example(scenario, idx):
    subject, raw_note, findings, recommendation, status = scenario
    dept = random.choice(DEPARTMENTS)
    prepared_by = random.choice(EMPLOYEES)
    approver = random.choice(APPROVER_TITLES)
    date = f"2026-{random.randint(1,9):02d}-{random.randint(1,28):02d}"
    instruction = random.choice(INSTRUCTION_VARIANTS)

    output = (
        f"APPROVAL NOTE\n\n"
        f"Subject: {subject}\n\n"
        f"Date: {date}\n"
        f"Prepared by: {prepared_by}\n"
        f"Department: {dept}\n\n"
        f"Findings:\n{findings}\n\n"
        f"Recommendation:\n{recommendation}\n\n"
        f"Approval Status: {status}\n\n"
        f"Signature: _______________\n"
        f"{approver}"
    )

    return {
        "instruction": instruction,
        "input": raw_note,
        "output": output,
    }


def main():
    examples = []
    # Cycle through scenarios multiple times with different random dept/employee/date
    # to reach ~60 examples while keeping the underlying situations realistic.
    target_count = 60
    i = 0
    while len(examples) < target_count:
        scenario = SCENARIOS[i % len(SCENARIOS)]
        examples.append(build_example(scenario, i))
        i += 1

    with open("training_data.jsonl", "w") as f:
        for ex in examples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")

    print(f"Wrote {len(examples)} examples to training_data.jsonl")


if __name__ == "__main__":
    main()
