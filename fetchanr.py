#!/usr/bin/env python2

import gzip, os, subprocess, sys, tempfile, uuid
import simplejson as json
import symbolicator

def runJob(job, dims, workdir, outfile, local=False):
    with tempfile.NamedTemporaryFile('w', suffix='.json', dir=workdir) as filterfile:
        filterfile.write(json.dumps({
            'version': 1,
            'dimensions': dims
        }))
        filterfile.flush()

        args = ['python', '-m', 'mapreduce.job',
                os.path.join(os.path.dirname(sys.argv[0]), job),
                '--input-filter', filterfile.name,
                '--num-mappers', '32',
                '--num-reducers', '8',
                '--data-dir', os.path.join(workdir, 'cache') if local else workdir,
                '--work-dir', workdir,
                '--output', outfile,
                '--bucket', 'telemetry-published-v1']
        if local:
            args.append('--local-only')

        env = os.environ
        print 'Calling %s' % (str(' '.join(args)))
        ret = subprocess.call(args, env=env)
        if ret:
            print 'Error %d' % (ret)
            sys.exit(ret)

def saveFile(outdir, name, index, data, prefix=''):
    fn = prefix + name + '.json.gz'
    with gzip.open(os.path.join(outdir, fn), 'wb') as outfile:
        outfile.write(json.dumps(data, separators=(',', ':')))
    index[name] = fn

def processDims(index, dims, allowed_infos, jobfile, outdir):
    mainthreads = {}
    backgroundthreads = {}
    slugs = {}
    dimsinfo = {}
    dimvalues = {}
    for line in jobfile:
        anr = json.loads(line.partition('\t')[2])
        slug = anr['slugs'][0][-1]
        slugs[slug] = anr['slugs']
        sym = None
        for t in anr['threads']:
            if 'native' not in t['name'].lower():
                continue
            if not sym:
                sym = symbolicator.Symbolicator.fromBuild(
                    os.path.dirname(jobfile.name), anr['symbolicatorInfo'])
                sym.fetchBinaries()
            t['stack'] = list(
                symbolicator.symbolicateStack(t['stack'], sym=sym))
        mainthread = next(t for t in anr['threads']
                          if t['name'] == anr['display'])
        mainthreads[slug] = [mainthread]
        backgroundthreads[slug] = [t for t in anr['threads']
                                   if t is not mainthread]
        info = anr['info']
        for dimname, infocounts in info.iteritems():
            for key, value in infocounts.iteritems():
                dimsinfo.setdefault(
                    dimname, {}).setdefault(slug, {})[key] = value
                dimvalues.setdefault(dimname, set()).add(key)
                for k, v in value.iteritems():
                    allowed_infos.setdefault(k, set()).update(v.iterkeys())

    saveFile(outdir, 'slugs', index, slugs)
    saveFile(outdir, 'main_thread', index, mainthreads)
    saveFile(outdir, 'background_threads', index, backgroundthreads)
    dummy_dict = {}
    for field, dim in dimsinfo.iteritems():
        next((d for d in dims if d['field_name'] == field), dummy_dict)[
            'allowed_values'] = list(dimvalues[field])
        saveFile(outdir, field, index['dimensions'], dim, prefix='dim_')

def processSessions(index, dims, allowed_infos, sessionsfile, outdir):
    sessions = {}
    def stripval(k, v):
        ret = {x: y for x, y in v.iteritems()
               if x in allowed_infos[k]}
        rest = sum(v.itervalues()) - sum(ret.itervalues())
        if rest:
            ret[""] = rest
        return ret
    for line in sessionsfile:
        parts = line.partition('\t')
        key = json.loads(parts[0])
        aggregate = {k: v for k, v
                     in json.loads(parts[2]).iteritems()
                     if k in allowed_infos}
        sessions.setdefault(key[0],
            {'uptime': {}})['uptime'][key[1]] = aggregate
    for fieldname, sessionsvalue in sessions.iteritems():
        saveFile(outdir, fieldname,
            index['sessions'], sessionsvalue, prefix='ses_')

def processBHR(index, jobfile, outdir):
    mainthreads = {}
    dimsinfo = {}
    sessions = {}
    count_lists = {}

    def adjustCounts(dim_vals, slug, count_lists):
        for dim_val, info_keys in dim_vals.iteritems():
            max_count = 0
            for info_vals in info_keys.itervalues():
                info_count = 0
                for val, counts in info_vals.iteritems():
                    count = sum(counts.itervalues())
                    info_vals[val] = count
                    info_count += count
                max_count = max(max_count, info_count)
            count_lists.setdefault(dim_val, []).append((slug, max_count))
        return dim_vals
    def mergeHangTime(dest, slug, dim_vals):
        for dim_val, info_keys in dim_vals.iteritems():
            dest_histogram = {}
            for info_vals in info_keys.itervalues():
                for time_histogram in info_vals.itervalues():
                    for time, counts in time_histogram.iteritems():
                        dest_histogram[time] = (counts +
                            dest_histogram.get(time, 0))
            dest.setdefault(dim_val, {}).setdefault(
                'name', {})[slug] = dest_histogram

    for line in jobfile:
        parts = line.partition('\t')
        stacks = json.loads(parts[0])
        stats = json.loads(parts[2])
        if stacks[0] is None:
            # uptime measurements
            tag = 'uptime'
            if stacks[1] is not None:
                tag += ':' + stacks[1]
            for k, v in stats.iteritems():
                for vk, vv in v.iteritems():
                    sessions.setdefault(k, {}).setdefault(tag, {})[vk] = vv
            continue
        if stacks[1] is None:
            # activity measurements
            tag = 'activity:' + stacks[0]
            for k, v in stats.iteritems():
                sessions.setdefault(k, {})[tag] = v
            continue
        # hang measurements
        stack = (['p:' + f for f in reversed(stacks[1])] +
                 ['p:' + stacks[0]])
        slug = str(uuid.uuid4())
        mainthreads[slug] = [{'name': 'main', 'stack': stack}]
        for k, v in stats.iteritems():
            mergeHangTime(sessions.setdefault(k, {})
                                  .setdefault('hangtime', {}), slug, v)
            dimsinfo.setdefault(k, {})[slug] = adjustCounts(
                v, slug, count_lists.setdefault(k, {}))

    slug_filter = []
    for dim_key, dim_vals in count_lists.iteritems():
        for dim_val, count_list in dim_vals.iteritems():
            count_list.sort(key=lambda x: x[1], reverse=True)
            slug_filter.extend(x[0] for x in count_list[:10])
    for slugs in dimsinfo.itervalues():
        for slug in list(slugs.iterkeys()):
            if slug not in slug_filter:
                del slugs[slug]
    for session in sessions.itervalues():
        for info_keys in session['hangtime'].itervalues():
            slugs = info_keys['name']
            for slug in list(slugs.iterkeys()):
                if slug not in slug_filter:
                    del slugs[slug]
    for slug in list(mainthreads.iterkeys()):
        if slug not in slug_filter:
            del mainthreads[slug]

    saveFile(outdir, 'main_thread', index, mainthreads)
    for field, dim in dimsinfo.iteritems():
        saveFile(outdir, field, index['dimensions'], dim, prefix='dim_')
    for fieldname, sessionsvalue in sessions.iteritems():
        saveFile(outdir, fieldname, index['sessions'], sessionsvalue, prefix='ses_')

if __name__ == '__main__':

    from datetime import datetime, timedelta

    if len(sys.argv) != 3:
        print 'Usage %s <from> <to>' % (sys.argv[0])
        sys.exit(1)

    DATE_FORMAT = '%Y%m%d'
    fromDate = datetime.strptime(sys.argv[1], DATE_FORMAT)
    toDate = datetime.strptime(sys.argv[2], DATE_FORMAT)

    if toDate < fromDate:
        print 'To date is less than from date'
        sys.exit(1)

    mindate = fromDate.strftime(DATE_FORMAT)
    maxdate = toDate.strftime(DATE_FORMAT)
    workdir = os.path.join('/mnt', 'tmp-anr-%s-%s' % (mindate, maxdate))
    worklocalonly = os.path.exists(os.path.join(workdir, 'cache'))
    if not os.path.exists(workdir):
        os.makedirs(workdir)

    sessionsdir = os.path.join('/mnt', 'tmp-sessions-%s-%s' % (mindate, maxdate))
    sessionlocalonly = os.path.exists(os.path.join(sessionsdir, 'cache'))
    if not os.path.exists(sessionsdir):
        os.makedirs(sessionsdir)

    outdir = os.path.join('/mnt', 'anr-%s-%s' % (mindate, maxdate))
    if not os.path.exists(outdir):
        os.makedirs(outdir)

    print 'Range: %s to %s' % (mindate, maxdate)
    print 'Work dir: %s' % workdir
    print 'Out dir: %s' % outdir
    if worklocalonly:
        print 'Local only'

    dims = [{
        'field_name': 'reason',
        'allowed_values': ['android-anr-report']
    }, {
        'field_name': 'appName',
        'allowed_values': '*'
    }, {
        'field_name': 'appUpdateChannel',
        'allowed_values': '*'
    }, {
        'field_name': 'appVersion',
        'allowed_values': '*'
    }, {
        'field_name': 'appBuildID',
        'allowed_values': '*'
    }, {
        'field_name': 'submission_date',
        'allowed_values': {
            'min': mindate,
            'max': maxdate
        }
    }]

    index = {
        'dimensions': {},
        'sessions': {},
    }
    allowed_infos = {}
    with tempfile.NamedTemporaryFile('r', suffix='.txt', dir=workdir) as outfile:
        runJob("mapreduce-anr.py", dims, workdir, outfile.name, local=worklocalonly)
        with open(outfile.name, 'r') as jobfile:
            processDims(index, dims, allowed_infos, jobfile, outdir)

    with tempfile.NamedTemporaryFile('r', suffix='.txt', dir=sessionsdir) as outfile:
        local = 'saved-session' in dims[0]['allowed_values'] or sessionlocalonly
        dims[0]['allowed_values'] = ['saved-session'];
        runJob("mapreduce-anr-sessions.py", dims, sessionsdir, outfile.name, local=local)
        with open(outfile.name, 'r') as sessionsfile:
            processSessions(index, dims, allowed_infos, sessionsfile, outdir)

    runJob("mapreduce-anr-summary.py", dims, sessionsdir,
           os.path.join(outdir, 'summary.txt'), local=True)

    with open(os.path.join(outdir, 'index.json'), 'w') as outfile:
        outfile.write(json.dumps(index, separators=(',', ':')))

    print 'Completed'

