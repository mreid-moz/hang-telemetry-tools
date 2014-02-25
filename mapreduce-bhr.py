# Same as the osdistribution.py example in jydoop
import simplejson as json
import mapreduce_common
import itertools

mapreduce_common.allowed_infos = mapreduce_common.allowed_infos_bhr
mapreduce_common.allowed_dimensions = mapreduce_common.allowed_dimensions_bhr

SKIP = 0

def map(raw_key, raw_dims, raw_value, cx):
    if SKIP > 0 and (hash(raw_key) % (SKIP + 1)) != 0:
        return
    if '"threadHangStats":' not in raw_value:
        return
    try:
        j = json.loads(raw_value)
        raw_sm = j['simpleMeasurements']
        uptime = raw_sm['uptime']
        if uptime < 0:
            return
        if raw_sm.get('debuggerAttached', 0):
            return
        raw_info = j['info']
        info = mapreduce_common.filterInfo(raw_info)
        mapreduce_common.addUptime(info, j)
        dims = mapreduce_common.filterDimensions(raw_dims, info)
    except KeyError:
        return

    def filterStack(stack):
        return (x[0] for x in itertools.groupby(stack))

    def collectData(dims, info, data):
        if isinstance(data, dict):
            data = {k: v for k, v in data.iteritems()
                    if v and k.isdigit()}
        return (1, {
            dim_key: {
                dim_val: {
                    info_key: {
                        info_val: data
                    }
                    for info_key, info_val in info.iteritems()
                }
            }
            for dim_key, dim_val in dims.iteritems()
        })
    collectedUptime = collectData(dims, info, uptime)

    for thread in j['threadHangStats']:
        name = thread['name']
        cx.write((name, None),
                 collectData(dims, info, thread['activity']))
        for hang in thread['hangs']:
            cx.write((name, tuple(filterStack(hang['stack']))),
                     collectData(dims, info, hang['histogram']))
        cx.write((None, name), collectedUptime)
    if j['threadHangStats']:
        cx.write((None, None), collectedUptime)

def do_combine(raw_key, raw_values):
    def merge_dict(left, right):
        for k, v in right.iteritems():
            if not isinstance(v, dict):
                left[k] = left.get(k, 0) + v
                continue
            if k not in left:
                left[k] = v
                continue
            merge_dict(left[k], v)
        return left
    def merge(left, right):
        return (left[0] + right[0], merge_dict(left[1], right[1]))
    return raw_key, reduce(merge, raw_values)

def combine(raw_key, raw_values, cx):
    key, value = do_combine(raw_key, raw_values)
    cx.write(key, value)

def reduce(raw_key, raw_values, cx):
    if (not raw_values or
        sum(x[0] for x in raw_values) < 10):
        return

    key, value = do_combine(raw_key, raw_values)
    cx.write(json.dumps(key, separators=(',', ':')),
             json.dumps(value[1], separators=(',', ':')))

