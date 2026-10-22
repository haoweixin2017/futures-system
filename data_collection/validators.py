import math
import statistics


def normalize_units(rows, mappings):
    result = []
    for row in rows:
        mapping = mappings[row['variety']]
        factor = mapping.get('conversion_factor')
        reliable = (isinstance(factor, (int, float)) and not isinstance(factor, bool) and math.isfinite(factor)
                    and factor > 0 and bool(mapping.get('conversion_basis')) and mapping.get('unit_raw') == row['unit_raw'])
        result.append(dict(row, unit_ton='吨' if reliable else None,
                           conversion_basis=mapping.get('conversion_basis') if reliable else 'UNVERIFIED_CONVERSION',
                           **{key + '_ton': row[key] * factor if reliable else None for key in ('warr_yesterday', 'warr_today', 'warr_chg')}))
    return result


def validate(rows, settings, previous=None, reference=None, history=None):
    errors, checks = [], {}
    totals = [r for r in rows if r.get('row_type') == 'TOTAL']
    if len(totals) != 1:
        return {'ok': False, 'errors': ['TOTAL_MISSING_OR_DUPLICATE'], 'checks': {}, 'total': None}
    total = totals[0]
    tolerance = settings.get('tolerance', 0)
    if any(r['warr_today'] < 0 or r['warr_yesterday'] < 0 for r in rows):
        errors.append('NEGATIVE_QUANTITY')
    if any(abs(r['warr_today'] - r['warr_yesterday'] - r['warr_chg']) > tolerance for r in rows):
        errors.append('ROW_CHANGE_MISMATCH')
    if previous is None:
        checks['cross_day'] = 'UNVERIFIED'
        if settings.get('require_previous'): errors.append('PREVIOUS_UNAVAILABLE')
    else:
        valid = previous['unit_raw'] == total['unit_raw'] and abs(previous['warr_today'] + total['warr_chg'] - total['warr_today']) <= tolerance
        checks['cross_day'] = 'PASS' if valid else 'FAIL'
        if not valid: errors.append('CROSS_DAY_MISMATCH')
    if reference is None:
        checks['reference'] = 'UNVERIFIED'
        if settings.get('require_reference'): errors.append('REFERENCE_UNAVAILABLE')
    else:
        value = total['warr_today'] if reference['unit'] == total['unit_raw'] else total.get('warr_today_ton') if reference['unit'] == '吨' else None
        valid = value is not None and abs(reference['value'] - value) <= tolerance
        checks['reference'] = 'PASS' if valid else 'FAIL'
        if not valid: errors.append('REFERENCE_MISMATCH')
    history = history or []
    checks['sigma'] = 'INSUFFICIENT_HISTORY'
    if len(history) >= settings.get('min_history', 20):
        deviation = statistics.pstdev(history)
        delta = abs(total['warr_today'] - statistics.mean(history))
        valid = delta <= max(tolerance, settings.get('sigma', 3) * deviation)
        checks['sigma'] = 'PASS' if valid else 'FAIL'
        if not valid: errors.append('SIGMA_OUTLIER')
    return {'ok': not errors, 'errors': errors, 'checks': checks, 'total': total['warr_today']}
