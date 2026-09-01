from __future__ import annotations

import datetime as dt
import bisect
import math
import statistics
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Europe/Amsterdam")
QUARTER_HOURS = 0.25


def _num(value, default=None):
    if isinstance(value, bool): return default
    try:
        x = float(value)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError): return default


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat()


def _parse(value: str) -> dt.datetime:
    x = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if x.tzinfo is None: x = x.replace(tzinfo=dt.timezone.utc)
    return x


def _capacity_kwh(value):
    x = _num(value)
    if x is None or x <= 0: return None
    return x / 1000.0 if x > 100 else x


def _windows(rows: list[dict], action: str | None = None) -> list[dict]:
    selected = [r for r in rows if action is None or r.get("action") == action]
    if not selected: return []
    selected = sorted(selected, key=lambda r: r["startUtc"])
    out=[]; current=None
    for row in selected:
        start=_parse(row["startUtc"]); end=_parse(row["endUtc"])
        if current and _parse(current["end"]) == start:
            current["end"]=_iso(end); current["quarters"]+=1
        else:
            current={"start":_iso(start),"end":_iso(end),"quarters":1}; out.append(current)
    return out


def _price_rows(price_days: list[dict]) -> list[dict]:
    out=[]
    for day in price_days:
        for r in day.get("rows") or []:
            price=_num(r.get("total_price_eur_kwh_incl_vat"))
            if price is None: continue
            market=_num(r.get("market_price_eur_kwh_ex_vat"))
            if market is None:
                market=_num(r.get("market_price_eur_kwh_incl_vat"))
            start=_parse(r["start_utc"]); end=start+dt.timedelta(minutes=15)
            out.append({"startUtc":_iso(start),"endUtc":_iso(end),"priceEurKwh":price,
                        "marketPriceEurKwh":market,
                        "deliveryDate":start.astimezone(LOCAL_TZ).date().isoformat()})
    return sorted(out,key=lambda x:x["startUtc"])


def _ev_plan(rows, current_soc, target_soc, capacity_kwh, charge_kw, efficiency, tolerance):
    result={"currentSocPercent":current_soc,"targetSocPercent":target_soc,"batteryCapacityKwh":capacity_kwh,
            "chargePowerKw":charge_kw,"chargeEfficiency":efficiency,"neededBatteryKwh":None,"neededGridKwh":None,
            "selectedQuarters":0,"windows":[],"practicalWindows":[],"averagePriceEurKwh":None,
            "minimumPriceEurKwh":None,"maximumPriceEurKwh":None,"estimatedCostEur":None,"assumptions":[]}
    if current_soc is None or capacity_kwh is None or charge_kw is None or charge_kw <= 0:
        result["assumptions"].append("EV planning requires reliable SoC, battery capacity and observed/configured charging power")
        return result, set()
    needed=max(0.0,(target_soc-current_soc)/100.0*capacity_kwh)
    grid=needed/max(efficiency,1e-9); result["neededBatteryKwh"]=needed; result["neededGridKwh"]=grid
    if grid <= 1e-9: return result,set()
    per=charge_kw*QUARTER_HOURS; count=min(len(rows),math.ceil(grid/per))
    # Equal-price quarters are deliberately selected as late as possible while
    # retaining the same cheapest achievable energy target.
    cheapest=sorted(range(len(rows)),key=lambda i:(rows[i]["priceEurKwh"], -_parse(rows[i]["startUtc"]).timestamp()))[:count]
    selected=set(cheapest); chosen=[rows[i] for i in sorted(selected)]
    result["selectedQuarters"]=len(chosen); result["windows"]=_windows([{**r,"action":"EV"} for r in chosen],"EV")
    prices=[r["priceEurKwh"] for r in chosen]
    energies=[]; remaining=grid
    for r in chosen:
        e=min(per,remaining); energies.append((r,e)); remaining-=e
    total=sum(e for _,e in energies)
    result["averagePriceEurKwh"]=sum(r["priceEurKwh"]*e for r,e in energies)/total if total else None
    result["minimumPriceEurKwh"]=min(prices) if prices else None; result["maximumPriceEurKwh"]=max(prices) if prices else None
    result["estimatedCostEur"]=sum(r["priceEurKwh"]*e for r,e in energies)
    # Practical alternative: cheapest contiguous block of equal quarter count, accepted when close to optimum.
    best=None
    for start in range(0,max(0,len(rows)-count+1)):
        block=rows[start:start+count]
        cost=sum(r["priceEurKwh"] for r in block)
        if best is None or cost<best[0]: best=(cost,block)
    if best:
        block_avg=best[0]/count; optimum=sum(prices)/len(prices)
        if block_avg <= optimum+tolerance:
            result["practicalWindows"]=_windows([{**r,"action":"EV"} for r in best[1]],"EV")
        else: result["practicalWindows"]=result["windows"]
    return result,selected


def _quarter_candidates(rows, ev_indices, cfg, demand_forecast, start_index=0, end_index=None):
    """Build independent self-consumption/export value segments per quarter."""
    if end_index is None:
        end_index = len(rows)
    discharge_eff = cfg["dischargeEfficiency"]
    reserve_w = int(cfg.get("gridImportReserveWatts", 100))
    max_discharge_w = int(cfg["maxDischargeWatts"])
    minimum_profit = max(0.0, _num(cfg.get("minimumProfitEurPerKwh"), 0.02))
    candidates = []
    for i in range(start_index, min(end_index, len(rows))):
        if i in ev_indices:
            continue
        row = rows[i]
        expected_kw = _num((demand_forecast or {}).get(row["startUtc"]), 0.0) or 0.0
        expected_import_w = max(0, int(round(expected_kw * 1000)))
        self_w = min(max_discharge_w, max(0, expected_import_w - reserve_w))
        self_value_per_battery = row["priceEurKwh"] * discharge_eff
        if self_w > 0 and row["priceEurKwh"] > minimum_profit:
            ac_kwh = self_w / 1000.0 * QUARTER_HOURS
            candidates.append({
                "index": i,
                "type": "SELF",
                "maxBatteryKwh": ac_kwh / max(discharge_eff, 1e-9),
                "valuePerBatteryKwh": self_value_per_battery,
                "priceEurKwh": row["priceEurKwh"],
            })

        export_price = _num(row.get("marketPriceEurKwh"))
        export_allowed = export_price is not None and export_price > minimum_profit
        export_w = max(0, max_discharge_w - self_w)
        if export_allowed and export_w > 0:
            ac_kwh = export_w / 1000.0 * QUARTER_HOURS
            candidates.append({
                "index": i,
                "type": "EXPORT",
                "maxBatteryKwh": ac_kwh / max(discharge_eff, 1e-9),
                "valuePerBatteryKwh": export_price * discharge_eff,
                "priceEurKwh": export_price,
            })
    return candidates


def _allocate_candidates(candidates, available_battery_kwh, minimum_value_per_battery=0.0):
    """Allocate a finite battery-energy budget to the highest-value segments first."""
    remaining = max(0.0, available_battery_kwh)
    allocations = []
    for candidate in sorted(
        candidates,
        key=lambda x: (x["valuePerBatteryKwh"], x["index"], x["type"] == "SELF"),
        reverse=True,
    ):
        if remaining <= 1e-12:
            break
        if candidate["valuePerBatteryKwh"] + 1e-12 < minimum_value_per_battery:
            continue
        battery_kwh = min(remaining, candidate["maxBatteryKwh"])
        if battery_kwh <= 1e-12:
            continue
        ac_kwh = battery_kwh * candidate.get("dischargeEfficiency", 1.0)
        # Candidates are expressed in battery kWh. Their economic value per battery
        # kWh already includes discharge efficiency, so value is direct here.
        allocations.append({**candidate, "batteryKwh": battery_kwh,
                            "valueEur": battery_kwh * candidate["valuePerBatteryKwh"]})
        remaining -= battery_kwh
    return allocations, remaining


def _charge_window(rows, ev_indices, cfg):
    """Return a contiguous low-price battery-charge window around the global minimum."""
    if not rows or int(cfg.get("maxChargeWatts", 0)) <= 0:
        return None
    # EV charging reserves quarters only against Venus discharge/export.
    # Cheap EV quarters remain valid candidates for charging Venus.
    eligible = list(range(len(rows)))
    if not eligible:
        return None
    cheapest = min(eligible, key=lambda i: (rows[i]["priceEurKwh"], -_parse(rows[i]["startUtc"]).timestamp()))
    delta = max(0.0, _num(cfg.get("chargeWindowPriceDeltaEurPerKwh"), 0.005))
    limit = rows[cheapest]["priceEurKwh"] + delta
    start = end = cheapest
    while start > 0 and rows[start - 1]["priceEurKwh"] <= limit:
        start -= 1
    while end + 1 < len(rows) and rows[end + 1]["priceEurKwh"] <= limit:
        end += 1
    indices = list(range(start, end + 1))
    if not indices:
        return None
    return {
        "startIndex": start,
        "endIndex": end,
        "indices": indices,
        "minimumPriceEurKwh": rows[cheapest]["priceEurKwh"],
        "maximumPriceEurKwh": max(rows[i]["priceEurKwh"] for i in indices),
        "priceLimitEurKwh": limit,
        "startUtc": rows[start]["startUtc"],
        "endUtc": rows[end]["endUtc"],
        "quarters": len(indices),
    }


def _battery_analytic(rows, ev_indices, soc_kwh, capacity_kwh, cfg, demand_forecast=None, solar_forecast=None):
    """Fast analytic battery planner.

    Prices/history determine *where* battery energy has the most value. Runtime P1
    and current SoC determine *how much* power is needed right now. No DP state
    expansion is used here.
    """
    min_kwh = capacity_kwh * cfg["minVenusSocPercent"] / 100.0
    max_kwh = capacity_kwh * cfg["maxVenusSocPercent"] / 100.0
    start_kwh = min(max(soc_kwh, min_kwh), max_kwh)
    charge_eff = cfg["chargeEfficiency"]
    discharge_eff = cfg["dischargeEfficiency"]
    max_charge_w = int(cfg["maxChargeWatts"])
    max_discharge_w = int(cfg["maxDischargeWatts"])
    spread = max(0.0, _num(cfg.get("minimumArbitrageSpreadEurPerKwh"), 0.10))
    minimum_profit = max(0.0, _num(cfg.get("minimumProfitEurPerKwh"), 0.02))
    reserve_w = int(cfg.get("gridImportReserveWatts", 100))

    annotated = []
    for i, source in enumerate(rows):
        row = dict(source)
        expected_kw = _num((demand_forecast or {}).get(row["startUtc"]), 0.0) or 0.0
        expected_import_w = max(0, int(round(expected_kw * 1000)))
        baseline_w = 0 if i in ev_indices else min(max_discharge_w, max(0, expected_import_w - reserve_w))
        export_price = _num(row.get("marketPriceEurKwh"))
        rejection = []
        if export_price is None:
            rejection.append("missing_entsoe_export_price")
        elif export_price <= minimum_profit:
            rejection.append("entsoe_export_value_not_above_minimum_profit")
        row.update({
            "expectedNetLoadKw": expected_kw,
            "expectedSolarPowerKw": 0.0,
            "reservedSolarCapacityKwh": 0.0,
            "baselineSelfConsumptionWatts": baseline_w,
            "economicExtraDischargeWatts": 0,
            "economicExportEligible": not rejection,
            "economicExportRejectionReasons": rejection,
            "economicExportValueEurPerKwh": export_price,
            "economicSelfValuePerBatteryKwh": row["priceEurKwh"] * discharge_eff,
            "economicSelfEligible": row["priceEurKwh"] > minimum_profit,
            "economicRechargeCostPerStoredKwh": None,
            "economicSelfMarginVsRechargeEurPerKwh": None,
            "economicExportMarginVsRechargeEurPerKwh": None,
            "economicDecision": "EV_HOLD" if i in ev_indices else "HOLD",
            "minimumArbitrageSpreadEurPerKwh": spread,
            "action": "EV_ACTIVE_HOLD" if i in ev_indices else "HOLD",
            "recommendedPowerWatts": 0,
            "plannedEnergyKwh": 0.0,
            "plannedSelfConsumptionKwh": 0.0,
            "plannedExportKwh": 0.0,
        })
        annotated.append(row)

    window = _charge_window(annotated, ev_indices, cfg)
    start_usable_battery_kwh = max(0.0, start_kwh - min_kwh)
    allocations = []
    charge_allocations = []
    pre_unused = start_usable_battery_kwh
    post_unused = 0.0
    actual_charge_cost_per_stored = None
    planned_charge_target_soc = None

    if window is None:
        # No grid charging: rank existing energy across the full known horizon.
        candidates = _quarter_candidates(annotated, ev_indices, cfg, demand_forecast)
        allocations, pre_unused = _allocate_candidates(candidates, start_usable_battery_kwh, 0.0)
    else:
        # A guaranteed cheap charge valley changes the value of battery headroom.
        # Energy discharged before the valley can be replaced inside that valley, so
        # pre-charge export is evaluated against its actual replacement cost instead
        # of being treated as irreversible use of the current battery inventory.
        window_average_price = statistics.fmean(annotated[i]["priceEurKwh"] for i in window["indices"])
        cheap_reference_cost = window_average_price / max(charge_eff, 1e-9)
        cycle_threshold = cheap_reference_cost + spread
        precharge_margin = max(0.0, _num(cfg.get("minimumPreChargeReplacementMarginEurPerKwh"), 0.01))
        precharge_floor_pct = max(0.0, min(100.0, _num(cfg.get("preChargeMinimumSocPercent"), cfg["minVenusSocPercent"])))
        precharge_floor_kwh = capacity_kwh * precharge_floor_pct / 100.0
        precharge_floor_kwh = min(precharge_floor_kwh, min_kwh)
        max_window_stored_kwh = len(window["indices"]) * max_charge_w / 1000.0 * QUARTER_HOURS * charge_eff

        window["averagePriceEurKwh"] = window_average_price
        window["referenceStoredCostEurPerKwh"] = cheap_reference_cost
        window["preChargeMinimumSocPercent"] = precharge_floor_pct
        window["minimumPreChargeReplacementMarginEurPerKwh"] = precharge_margin
        window["maximumStoredChargeKwh"] = max_window_stored_kwh
        for row in annotated:
            export_value = _num(row.get("economicExportValueEurPerKwh"))
            row["economicRechargeCostPerStoredKwh"] = cheap_reference_cost
            row["economicSelfMarginVsRechargeEurPerKwh"] = row["economicSelfValuePerBatteryKwh"] - cheap_reference_cost
            row["economicExportMarginVsRechargeEurPerKwh"] = (
                None if export_value is None else export_value * discharge_eff - cheap_reference_cost
            )
            row["preChargeMinimumSocPercent"] = precharge_floor_pct
            row["minimumPreChargeReplacementMarginEurPerKwh"] = precharge_margin
            row["selectedForPreChargeExport"] = False

        post_candidates = _quarter_candidates(
            annotated, ev_indices, cfg, demand_forecast, window["endIndex"] + 1, len(annotated)
        )
        profitable_post = [c for c in post_candidates if c["valuePerBatteryKwh"] + 1e-12 >= cycle_threshold]
        profitable_post_demand = sum(c["maxBatteryKwh"] for c in profitable_post)
        desired_post_battery = min(max_kwh - min_kwh, profitable_post_demand)

        pre_candidates = _quarter_candidates(
            annotated, ev_indices, cfg, demand_forecast, 0, window["startIndex"]
        )
        # SELF keeps the normal arbitrage threshold. EXPORT gets a dedicated
        # replacement-margin threshold: when a cheap valley can refill the battery,
        # profitable export creates valuable headroom and may take SoC down to the
        # configured pre-charge floor (5% by default).
        selected_pre_candidates = []
        for c in pre_candidates:
            threshold = cycle_threshold if c["type"] == "SELF" else cheap_reference_cost + precharge_margin
            if c["valuePerBatteryKwh"] + 1e-12 >= threshold:
                selected_pre_candidates.append(c)

        pre_available_to_floor = max(0.0, start_kwh - precharge_floor_kwh)
        # Do not create more headroom than the cheap window can refill. Existing
        # normal-minimum headroom is already available without extra discharge.
        existing_headroom = max(0.0, max_kwh - start_kwh)
        refillable_extra_headroom = max(0.0, max_window_stored_kwh - existing_headroom)
        pre_budget = min(pre_available_to_floor, start_usable_battery_kwh + refillable_extra_headroom)
        pre_alloc, pre_unused = _allocate_candidates(selected_pre_candidates, pre_budget, 0.0)
        allocations.extend(pre_alloc)
        pre_removed = sum(a["batteryKwh"] for a in pre_alloc)
        soc_before_charge = max(precharge_floor_kwh, start_kwh - pre_removed)

        for a in pre_alloc:
            if a["type"] == "EXPORT":
                annotated[a["index"]]["selectedForPreChargeExport"] = True

        carried_usable_post_kwh = max(0.0, soc_before_charge - min_kwh)
        required_new_stored = max(0.0, desired_post_battery - carried_usable_post_kwh)
        # If we deliberately went below the normal minimum before the guaranteed
        # charge valley, first restore that temporary reserve deficit.
        reserve_restore_kwh = max(0.0, min_kwh - soc_before_charge)
        required_new_stored += reserve_restore_kwh
        required_new_stored = min(required_new_stored, max_kwh - soc_before_charge, max_window_stored_kwh)

        if required_new_stored > 1e-12 and window["indices"]:
            remaining = required_new_stored
            for pos, i in enumerate(window["indices"]):
                remaining_quarters = len(window["indices"]) - pos
                if remaining <= 1e-12:
                    break
                equal_stored = remaining / remaining_quarters
                max_stored = max_charge_w / 1000.0 * QUARTER_HOURS * charge_eff
                stored = min(remaining, equal_stored, max_stored)
                grid = stored / max(charge_eff, 1e-9)
                charge_allocations.append({"index": i, "storedKwh": stored, "gridKwh": grid,
                                           "costEur": grid * annotated[i]["priceEurKwh"]})
                remaining -= stored
            total_stored = sum(a["storedKwh"] for a in charge_allocations)
            total_cost = sum(a["costEur"] for a in charge_allocations)
            actual_charge_cost_per_stored = total_cost / total_stored if total_stored else None
            soc_after_charge = min(max_kwh, soc_before_charge + total_stored)
        else:
            soc_after_charge = soc_before_charge

        planned_charge_target_soc = soc_after_charge / capacity_kwh * 100.0
        window["projectedSocAtStartPercent"] = soc_before_charge / capacity_kwh * 100.0
        window["preChargeTargetSocPercent"] = soc_before_charge / capacity_kwh * 100.0
        window["plannedChargeTargetSocPercent"] = planned_charge_target_soc
        window["plannedStoredChargeKwh"] = max(0.0, soc_after_charge - soc_before_charge)
        window["requiredHeadroomKwh"] = max(0.0, start_kwh - soc_before_charge)
        window["plannedPreChargeExportKwh"] = sum(
            a["batteryKwh"] * discharge_eff for a in pre_alloc if a["type"] == "EXPORT"
        )

        post_available = max(0.0, soc_after_charge - min_kwh)
        post_threshold = (actual_charge_cost_per_stored if actual_charge_cost_per_stored is not None
                          else cheap_reference_cost) + spread
        post_alloc, post_unused = _allocate_candidates(post_candidates, post_available, post_threshold)
        allocations.extend(post_alloc)

    # Merge value-segment allocations into chronological quarter rows.
    by_index = {}
    for a in allocations:
        item = by_index.setdefault(a["index"], {"selfBatteryKwh": 0.0, "exportBatteryKwh": 0.0})
        key = "selfBatteryKwh" if a["type"] == "SELF" else "exportBatteryKwh"
        item[key] += a["batteryKwh"]
    charge_by_index = {a["index"]: a for a in charge_allocations}

    soc = start_kwh
    charge_cost = avoided = export_revenue = charged = discharged = exported = 0.0
    for i, row in enumerate(annotated):
        allocation = by_index.get(i)
        if allocation:
            self_battery = allocation["selfBatteryKwh"]
            export_battery = allocation["exportBatteryKwh"]
            total_battery = self_battery + export_battery
            self_ac = self_battery * discharge_eff
            export_ac = export_battery * discharge_eff
            total_ac = self_ac + export_ac
            power = int(round(total_ac / QUARTER_HOURS * 1000.0))
            power = min(max_discharge_w, power)
            row["action"] = "VENUS_DISCHARGE"
            row["recommendedPowerWatts"] = power
            row["plannedEnergyKwh"] = total_ac
            row["plannedSelfConsumptionKwh"] = self_ac
            row["plannedExportKwh"] = export_ac
            row["baselineSelfConsumptionWatts"] = int(round(self_ac / QUARTER_HOURS * 1000.0))
            row["economicExtraDischargeWatts"] = int(round(export_ac / QUARTER_HOURS * 1000.0))
            row["dischargeFloorSocPercent"] = (
                window.get("preChargeMinimumSocPercent", cfg["minVenusSocPercent"])
                if window and i < window["startIndex"] else cfg["minVenusSocPercent"]
            )
            if self_ac > 1e-12 and export_ac > 1e-12:
                row["economicDecision"] = "SELF+EXPORT"
            elif export_ac > 1e-12:
                row["economicDecision"] = "EXPORT"
            else:
                row["economicDecision"] = "SELF"
            row_floor_soc = _num(row.get("dischargeFloorSocPercent"), cfg["minVenusSocPercent"])
            row_floor_kwh = capacity_kwh * row_floor_soc / 100.0
            soc = max(row_floor_kwh, soc - total_battery)
            discharged += total_ac
            exported += export_ac
            avoided += self_ac * row["priceEurKwh"]
            export_revenue += export_ac * (_num(row.get("marketPriceEurKwh"), 0.0) or 0.0)

        charge = charge_by_index.get(i)
        if charge and row["action"] in {"HOLD", "EV_ACTIVE_HOLD"}:
            row["action"] = "VENUS_CHARGE"
            grid_w = int(round(charge["gridKwh"] / QUARTER_HOURS * 1000.0))
            row["recommendedPowerWatts"] = -min(max_charge_w, grid_w)
            row["plannedEnergyKwh"] = charge["gridKwh"]
            row["plannedStoredEnergyKwh"] = charge["storedKwh"]
            row["chargeTargetSocPercent"] = planned_charge_target_soc if planned_charge_target_soc is not None else (soc + charge["storedKwh"]) / capacity_kwh * 100.0
            row["solarAbsorbTargetSocPercent"] = cfg["maxVenusSocPercent"]
            row["economicDecision"] = "GRID_CHARGE"
            row["chargeWindowStartUtc"] = window["startUtc"] if window else row["startUtc"]
            row["chargeWindowEndUtc"] = window["endUtc"] if window else row["endUtc"]
            soc = min(max_kwh, soc + charge["storedKwh"])
            charged += charge["gridKwh"]
            charge_cost += charge["costEur"]

        row["expectedSocPercentAfter"] = soc / capacity_kwh * 100.0
        row["expectedSocAfterPercent"] = row["expectedSocPercentAfter"]

    return {
        "plannerMode": "analytic-ranked-v1.2-precharge-headroom",
        "plan": annotated,
        "startSocPercent": start_kwh / capacity_kwh * 100.0,
        "endSocPercent": soc / capacity_kwh * 100.0,
        "effectiveMinimumSocPercent": min_kwh / capacity_kwh * 100.0,
        "reserveEnergyKwh": min_kwh,
        "availableDischargeEnergyKwh": start_usable_battery_kwh * discharge_eff,
        "plannedChargeEnergyKwh": charged,
        "plannedDischargeEnergyKwh": discharged,
        "plannedExportEnergyKwh": exported,
        "unusedAvailableDischargeEnergyKwh": max(0.0, pre_unused * discharge_eff),
        "postChargeUnusedBatteryEnergyKwh": max(0.0, post_unused),
        "chargedKwh": charged,
        "dischargedKwh": discharged,
        "exportedKwh": exported,
        "chargingCostEur": charge_cost,
        "avoidedImportEur": avoided,
        "exportRevenueEur": export_revenue,
        "expectedArbitrageProfitEur": avoided + export_revenue - charge_cost,
        "minimumArbitrageSpreadEurPerKwh": spread,
        "chargeWindow": window,
        "averageChargeCostPerStoredKwh": actual_charge_cost_per_stored,
        "forecastSolarEnergyKwh": 0.0,
        "chargeWindows": _windows(annotated, "VENUS_CHARGE"),
        "dischargeWindows": _windows(annotated, "VENUS_DISCHARGE"),
        "exportWindows": _windows(
            [{**r, "action": "VENUS_EXPORT"} for r in annotated if r.get("plannedExportKwh", 0.0) > 1e-9],
            "VENUS_EXPORT",
        ),
        "holdWindows": _windows([r for r in annotated if r["action"] in {"HOLD", "EV_ACTIVE_HOLD"}]),
    }


# Temporary compatibility for existing tests/internal callers while the planner
# implementation is no longer a dynamic-programming state search.
def _battery_dp(rows, ev_indices, soc_kwh, capacity_kwh, cfg, demand_forecast=None, solar_forecast=None):
    return _battery_analytic(rows, ev_indices, soc_kwh, capacity_kwh, cfg, demand_forecast, solar_forecast)

def build_plan(price_days, overview, venus_state, cfg, observed_ev_charge_kw=None, target_soc=None, demand_forecast=None, forecast_meta=None, solar_forecast=None, solar_meta=None):
    all_rows=_price_rows(price_days); today=dt.datetime.now(LOCAL_TZ).date(); tomorrow=today+dt.timedelta(days=1)
    counts={d:sum(1 for r in all_rows if r["deliveryDate"]==d.isoformat()) for d in (today,tomorrow)}
    now=dt.datetime.now(dt.timezone.utc)
    rows=[r for r in all_rows if _parse(r["endUtc"]) > now]
    if not rows: return {"ok":False,"error":"missing_prices","reason":"Geen EasyEnergy kwartierprijzen beschikbaar","horizon":{"todayQuarters":0,"tomorrowQuarters":0}}
    energy=(overview or {}).get("energy") or {}; vehicle=(overview or {}).get("vehicle") or {}
    current=_num(energy.get("batteryPercent")); target=_num(target_soc,cfg["defaultEvTargetSocPercent"])
    if target<0 or target>100: raise ValueError("targetEvSoc must be between 0 and 100")
    cap=_capacity_kwh(vehicle.get("batteryCapacityKwh")); charge_kw=_num(energy.get("chargingPowerKw")) or _num(observed_ev_charge_kw)
    ev,ev_indices=_ev_plan(rows,current,target,cap,charge_kw,cfg["evChargeEfficiency"],cfg["practicalWindowPriceToleranceEurPerKwh"])
    vcap=_capacity_kwh((venus_state or {}).get("ratedCapacity")) or _capacity_kwh((venus_state or {}).get("batteryCapacity"))
    vsoc=_num((venus_state or {}).get("soc"))
    venus={"currentSocPercent":vsoc,"batteryCapacityKwh":vcap,"maxChargeWatts":cfg["maxChargeWatts"],"maxDischargeWatts":cfg["maxDischargeWatts"],
           "startSocPercent":vsoc,"endSocPercent":vsoc,"chargedKwh":0.0,"dischargedKwh":0.0,"exportedKwh":0.0,"chargingCostEur":0.0,"avoidedImportEur":0.0,"exportRevenueEur":0.0,"expectedArbitrageProfitEur":0.0,"chargeWindows":[],"dischargeWindows":[],"exportWindows":[],"holdWindows":[],"plan":[]}
    reserve_active = counts[tomorrow] == 0
    effective_cfg = dict(cfg)
    if reserve_active:
        effective_cfg["minVenusSocPercent"] = max(
            cfg["minVenusSocPercent"],
            _num(cfg.get("missingTomorrowReserveSocPercent"), 60),
        )
    if vcap and vsoc is not None:
        venus.update(_battery_analytic(rows,ev_indices,vcap*vsoc/100,vcap,effective_cfg,demand_forecast,solar_forecast))
    solar_quarters = {
        _iso(_parse(q["startUtc"])): q for q in (solar_meta or {}).get("quarterForecast", [])
        if isinstance(q, dict) and q.get("startUtc")
    }
    output_quarters = venus.get("plan") or rows
    for quarter in output_quarters:
        solar_quarter = solar_quarters.get(_iso(_parse(quarter["startUtc"])))
        if solar_quarter:
            quarter["globalTiltedIrradianceWm2"] = solar_quarter.get("globalTiltedIrradianceWm2")
            quarter["sunExpected"] = bool(solar_quarter.get("sunExpected"))
    assumptions={"chargeEfficiency":cfg["chargeEfficiency"],"dischargeEfficiency":cfg["dischargeEfficiency"],"evChargeEfficiency":cfg["evChargeEfficiency"],
                 "minimumProfitEurPerKwh":cfg["minimumProfitEurPerKwh"],
                 "minimumArbitrageSpreadEurPerKwh":_num(cfg.get("minimumArbitrageSpreadEurPerKwh"),0.10),
                 "exportPriceSource":"ENTSO-E market_price_eur_kwh_ex_vat",
                 "solarForecastAvailable":bool(solar_quarters),
                 "historicalP1Forecast": bool(demand_forecast),
                 "forecastMeta": forecast_meta or {},
                 "solarForecastMeta": solar_meta or {},
                 "tomorrowPricesAvailable": counts[tomorrow] > 0,
                 "missingTomorrowReserveActive": reserve_active,
                 "effectiveMinimumVenusSocPercent": effective_cfg["minVenusSocPercent"],
                 "powerQuantumWatts": int(cfg.get("planningPowerQuantumWatts",50)),
                 "plannerMode":"analytic-ranked-v1.2-precharge-headroom",
                 "note":"Planning ranks price/P1 value analytically; runtime decisions use live P1 and live Venus SoC. Solar forecast is not required. When tomorrow prices are unavailable a conservative Venus SoC reserve is kept."}
    return {"ok":True,"readOnly":True,"generatedAtUtc":dt.datetime.now(dt.timezone.utc).isoformat(),
            "horizon":{"todayQuarters":counts[today],"tomorrowQuarters":counts[tomorrow],"totalQuarters":len(all_rows),"remainingQuarters":len(rows)},
            "volvo":ev,"venus":venus,"assumptions":assumptions,"quarters":output_quarters}


def _phase_w(latest, phase):
    imp=_num(latest.get(f"import_l{phase}_kw"),0.0); exp=_num(latest.get(f"export_l{phase}_kw"),0.0)
    return (imp-exp)*1000


def evaluate_runtime(plan, overview, p1, venus_state, cfg, p1_recent=None):
    latest=(p1 or {}).get("latest") or {}; now=dt.datetime.now(dt.timezone.utc)
    phase={f"l{i}Watts":_phase_w(latest,i) for i in (1,2,3)}
    total_net_watts=sum(phase.values())
    samples=[]
    for r in (p1_recent or [])[-int(cfg["runtimeSmoothingSamples"]):]:
        if any(k in r for k in ("import_l2_kw","export_l2_kw")):
            samples.append(_phase_w(r,2))
    l2_smoothed=statistics.fmean(samples) if samples else phase["l2Watts"]
    energy=(overview or {}).get("energy") or {}; status=str(energy.get("chargingStatus") or "").upper()
    volvo_charging=status in {"CHARGING","CHARGE","ACTIVE"}
    threshold=cfg["evThreePhaseDetectionMinWattsPerPhase"]
    three_phase=all(phase[f"l{i}Watts"]>=threshold for i in (1,2,3))
    if (p1 or {}).get("stale") or not latest:
        return _runtime("SAFE_HOLD",0,"P1 stale: hold",plan,phase,l2_smoothed,volvo_charging,True)
    if not venus_state or not venus_state.get("online") or venus_state.get("stale") or _num(venus_state.get("soc")) is None:
        return _runtime("SAFE_HOLD",0,"Venus offline/stale: no action",plan,phase,l2_smoothed,volvo_charging,True)
    if not plan or not plan.get("ok"):
        return _runtime("SAFE_HOLD",0,"Price plan unavailable: hold",plan,phase,l2_smoothed,volvo_charging,True)

    current=None
    for q in plan.get("quarters") or []:
        if _parse(q["startUtc"])<=now<_parse(q["endUtc"]):
            current=q; break
    planned=(current or {}).get("action","HOLD")

    if volvo_charging and planned == "VENUS_DISCHARGE":
        reason="EV charging active" if three_phase else "EV charging reported; P1 profile not yet conclusive - conservative hold"
        return _runtime("EV_ACTIVE_HOLD",0,reason,plan,phase,l2_smoothed,True,not three_phase)
    if three_phase and not volvo_charging:
        return _runtime("SAFE_HOLD",0,"3-phase charging profile without Volvo charging status: inconsistency",plan,phase,l2_smoothed,False,True)
    soc=_num(venus_state.get("soc"),0.0)
    minimum_setpoint=max(25,int(cfg.get("minimumBatterySetpointWatts",100)))
    max_charge=int(cfg["maxChargeWatts"])
    max_discharge=int(cfg["maxDischargeWatts"])
    reserve_w=int(cfg.get("gridImportReserveWatts",100))

    # Reconstruct the house/PV balance before the battery. P1 is measured after
    # battery power, while Marstek uses positive=discharge and negative=charge.
    actual_battery_power=int(round(_num(venus_state.get("ongridPower"),0.0) or 0.0))
    site_net_without_battery_w=total_net_watts+actual_battery_power
    solar_surplus_w=max(0,-site_net_without_battery_w)
    live_import_without_battery_w=max(0,site_net_without_battery_w)

    discharge_floor_soc=_num((current or {}).get("dischargeFloorSocPercent"),cfg.get("minVenusSocPercent",0.0))
    if planned == "VENUS_DISCHARGE" and soc <= discharge_floor_soc:
        return _runtime("HOLD",0,"Battery discharge floor SoC reached: hold",plan,phase,l2_smoothed,False,True,
                        {"solarSurplusWatts":solar_surplus_w,"siteNetWithoutBatteryWatts":site_net_without_battery_w,
                         "dischargeFloorSocPercent":discharge_floor_soc})

    # During a planned cheap-price window, recalculate only the current setpoint
    # from live SoC and time remaining. Solar does not reduce the battery setpoint;
    # it merely supplies part/all of that charging. Surplus above the base request
    # is absorbed when possible.
    if planned == "VENUS_CHARGE":
        target_soc=_num((current or {}).get("chargeTargetSocPercent"),cfg.get("maxVenusSocPercent",100.0))
        solar_target_soc=_num((current or {}).get("solarAbsorbTargetSocPercent"),cfg.get("maxVenusSocPercent",100.0))
        if soc >= target_soc-1e-9:
            if soc < solar_target_soc-1e-9 and solar_surplus_w >= minimum_setpoint and bool(venus_state.get("chargFlag",True)):
                absorb=min(max_charge,solar_surplus_w)
                expected_grid_w=site_net_without_battery_w+absorb
                return _runtime("SOLAR_ABSORB",-absorb,"Economic grid-charge target reached; absorbing live solar surplus",plan,phase,l2_smoothed,False,True,
                                {"solarSurplusWatts":solar_surplus_w,"baseChargeWatts":0,
                                 "chargeTargetSocPercent":target_soc,"solarAbsorbTargetSocPercent":solar_target_soc,
                                 "expectedGridImportWatts":max(0,expected_grid_w),"expectedGridExportWatts":max(0,-expected_grid_w),
                                 "siteNetWithoutBatteryWatts":site_net_without_battery_w})
            return _runtime("HOLD",0,"Economic grid-charge target SoC reached",plan,phase,l2_smoothed,False,False,
                            {"solarSurplusWatts":solar_surplus_w,"baseChargeWatts":0,
                             "chargeTargetSocPercent":target_soc,"solarAbsorbTargetSocPercent":solar_target_soc,
                             "expectedGridImportWatts":max(0,site_net_without_battery_w),
                             "expectedGridExportWatts":max(0,-site_net_without_battery_w),
                             "siteNetWithoutBatteryWatts":site_net_without_battery_w})
        capacity_kwh=_capacity_kwh(venus_state.get("ratedCapacity")) or _capacity_kwh(venus_state.get("batteryCapacity"))
        window_end=_parse((current or {}).get("chargeWindowEndUtc") or current["endUtc"])
        seconds_remaining=max(0.0,(window_end-now).total_seconds())
        if not capacity_kwh or seconds_remaining <= 0:
            return _runtime("HOLD",0,"Charge window ended or battery capacity unavailable",plan,phase,l2_smoothed,False,True)
        missing_stored_kwh=max(0.0,capacity_kwh*(target_soc-soc)/100.0)
        required_ac_w=missing_stored_kwh/(seconds_remaining/3600.0)/max(cfg["chargeEfficiency"],1e-9)*1000.0
        base_charge_w=min(max_charge,max(0,int(round(required_ac_w))))
        if 0 < base_charge_w < minimum_setpoint:
            base_charge_w=minimum_setpoint
        absorb_w=solar_surplus_w if solar_surplus_w >= minimum_setpoint and bool(venus_state.get("chargFlag",True)) else 0
        battery_charge_w=min(max_charge,max(base_charge_w,absorb_w))
        expected_grid_w=site_net_without_battery_w+battery_charge_w
        details={
            "baseChargeWatts":base_charge_w,
            "solarSurplusWatts":solar_surplus_w,
            "siteNetWithoutBatteryWatts":site_net_without_battery_w,
            "expectedGridImportWatts":max(0,expected_grid_w),
            "expectedGridExportWatts":max(0,-expected_grid_w),
            "chargeTargetSocPercent":target_soc,
            "solarAbsorbTargetSocPercent":solar_target_soc,
            "chargeWindowEndUtc":_iso(window_end),
            "remainingChargeEnergyKwh":missing_stored_kwh,
            "remainingChargeSeconds":int(seconds_remaining),
        }
        action="SOLAR_ABSORB" if absorb_w>base_charge_w else "VENUS_CHARGE"
        reason="Cheap charge window with live solar absorb" if action=="SOLAR_ABSORB" else "Cheap charge window: live SoC/deadline setpoint"
        return _runtime(action,-battery_charge_w,reason,plan,phase,l2_smoothed,False,action!=planned,details)

    # Outside a deliberate discharge quarter, absorb actual P1/PV export directly.
    if planned in {"HOLD", "EV_ACTIVE_HOLD"} and solar_surplus_w >= minimum_setpoint and bool(venus_state.get("chargFlag",True)):
        target_soc=_num(cfg.get("maxVenusSocPercent"),100.0)
        if soc < target_soc-1e-9:
            absorb=min(max_charge,solar_surplus_w)
            return _runtime("SOLAR_ABSORB",-absorb,"P1 export surplus available",plan,phase,l2_smoothed,False,planned!="SOLAR_ABSORB",
                            {"solarSurplusWatts":solar_surplus_w,"baseChargeWatts":0,
                             "siteNetWithoutBatteryWatts":site_net_without_battery_w,
                             "expectedGridImportWatts":0,
                             "expectedGridExportWatts":max(0,solar_surplus_w-absorb)})

    if planned == "VENUS_DISCHARGE":
        # The economic planner already allocated a fixed battery setpoint for this
        # quarter (forecast self-consumption + any deliberate export). Do not
        # re-plan that energy every few seconds from live P1 load.
        planned_quarter_w=max(0,int(round(_num((current or {}).get("recommendedPowerWatts"),0.0) or 0.0)))
        power=min(max_discharge,planned_quarter_w)
        if power < minimum_setpoint:
            power=0

        # Keep live P1 reconstruction as telemetry only. It may be used for
        # diagnostics/safety, but it must not alter the discharge setpoint.
        live_self_w=min(max_discharge,max(0,live_import_without_battery_w-reserve_w))
        planned_extra_raw=_num((current or {}).get("economicExtraDischargeWatts"))
        if planned_extra_raw is None:
            planned_export_kwh=max(0.0,_num((current or {}).get("plannedExportKwh"),0.0) or 0.0)
            planned_extra_raw=planned_export_kwh/QUARTER_HOURS*1000.0
        planned_extra=max(0,int(round(planned_extra_raw)))

        details={
            "siteNetWithoutBatteryWatts":site_net_without_battery_w,
            "liveSelfConsumptionWatts":live_self_w,
            "plannedQuarterPowerWatts":planned_quarter_w,
            "plannedEconomicExportWatts":planned_extra,
            "solarSurplusWatts":solar_surplus_w,
            "dischargeFloorSocPercent":discharge_floor_soc,
            "economicExportMarginVsRechargeEurPerKwh":_num((current or {}).get("economicExportMarginVsRechargeEurPerKwh")),
            "economicRechargeCostPerStoredKwh":_num((current or {}).get("economicRechargeCostPerStoredKwh")),
            "selectedForPreChargeExport":bool((current or {}).get("selectedForPreChargeExport",False)),
        }
        return _runtime("VENUS_DISCHARGE",power,"Economic quarter: fixed planned setpoint",plan,phase,l2_smoothed,False,False,details)

    return _runtime(planned,0,"Hold for better economic quarter",plan,phase,l2_smoothed,False,False,
                    {"solarSurplusWatts":solar_surplus_w,"siteNetWithoutBatteryWatts":site_net_without_battery_w})


def _runtime(action,power,reason,plan,phase,l2,charging,recalc,details=None):
    now=dt.datetime.now(dt.timezone.utc); current=None; future=[]
    if plan:
        for q in plan.get("quarters") or []:
            start=_parse(q["startUtc"]); end=_parse(q["endUtc"])
            if start<=now<end: current=q
            if start>now: future.append(q)
    current_price=(current or {}).get("priceEurKwh")
    better=None
    if current_price is not None:
        candidates=[q for q in future if q.get("priceEurKwh") is not None and q["priceEurKwh"]>current_price]
        if candidates: better=max(candidates,key=lambda q:q["priceEurKwh"])
    planned=(current or {}).get("action","HOLD")
    result={"ok":True,"readOnly":True,"plannedAction":planned,"recommendedActionNow":action,
            "recommendedPowerWatts":power,"reason":reason,"volvoCharging":charging,"p1":{**phase,"l2SmoothedWatts":l2},
            "currentPrice":current_price,"nextBetterPrice":None if not better else {"startUtc":better["startUtc"],"priceEurKwh":better["priceEurKwh"]},
            "stale":action=="SAFE_HOLD","shouldRecalculate":recalc,"deviationFromPlan":recalc or action!=planned}
    if details:
        result.update(details)
    return result
