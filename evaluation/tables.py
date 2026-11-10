"""The screening and validation tables printed in the paper."""
import csv
from collections import Counter
from datetime import datetime

from evaluation.common import DATA, VALIDATION, read_data, report
from evaluation.analyze import wilson


def screening(corpus):
    data = read_data('screening')
    gates = data['gate_records']
    records = {r['step']: r for r in data['step_records'] if not r.get('cache')}
    routes = {route: {'input':sum(r['route']==route for r in gates),
        'kept':sum(r['route']==route and r['status']=='kept' for r in gates)}
        for route in ['term','citation','dataset']}
    outcomes = Counter(r['outcome'] for r in data['dataset_outcomes'])
    return {'search_hits':records['1.1_initial_search']['output_count'], 'routes':routes,
        'candidates':len(gates),'kept':sum(r['status']=='kept' for r in gates),
        'errors':sum(r['status']=='error' for r in gates),
        'papers':corpus['papers_n'],'papers_without_link':sum(r['status']=='kept' for r in gates)-corpus['papers_n'],
        'text_source':corpus['papers_text_source'],
        'papers_with_task':sum(bool(r['task']) for r in read_data('papers')),
        'metric_rows_generated':corpus['metrics_rows_raw'],'metric_rows_on_hub':corpus['metrics']['rows'],
        'dataset_names':records['1.5_build_dataset_seed']['output_count'],
        'canonical_datasets':records['1.5b_dataset_dedupe']['output_count'],
        'pathology_input':records['2.0_prevalidation']['input_count'],
        'pathology_kept':records['2.0_prevalidation']['output_count'],
        'dataset_outcomes':dict(outcomes),'catalog_records':corpus['catalog_n'],
        'operational_groups':corpus['catalog_merge']['merged_datasets'],
        'paper_dataset_pairs':corpus['links']['paper_dataset_pairs']}


def operations():
    data=read_data('operations')
    events=data['events']
    starts=[datetime.fromisoformat(s['started_at']) for s in data['sessions'] if s.get('started_at')]
    ends=[datetime.fromisoformat(s['ended_at']) for s in data['sessions'] if s.get('ended_at')]
    return {'elapsed_hours':(max(ends)-min(starts)).total_seconds()/3600,
        'sessions':len(data['sessions']),
        'answered_model_calls':sum(r['kind']=='llm' for r in events),
        'default_tier_fallbacks':sum(r['kind']=='flex_fallback' for r in events),
        'text_loads':dict(Counter(r.get('source') for r in events if r['kind']=='fetch'))}


def rate(k,n,units=None,interval=True):
    p,lo,hi=wilson(k,n)
    row={'k':k,'n':n,'units':units if units is not None else n,'p':p}
    if interval:row.update(lo=lo,hi=hi)
    return row


def development_rows(d):
    gate=report('relevance_rescore')['gates']['full-text gate, structured (flag)']
    tasks=report('task_rescore')['current']
    pathology=report('validation_rescore')['rows']
    use=report('dataset_usage_rescore')
    tally=use['tally']
    return [
        ('Gate','relevant papers kept',rate(gate['tp'],gate['tp']+gate['fn'])),
        ('Gate','irrelevant papers rejected',rate(gate['tn'],gate['tn']+gate['fp'])),
        ('Extraction','field matches labeled value',d['extraction']['positive_labels']),
        ('Extraction','tissue site',d['extraction']['fields_positive_labels']['tissue_site']),
        ('Extraction','WSI versus patch',d['extraction']['fields_positive_labels']['wsi_or_patch']),
        ('Extraction','avoids value labeled wrong',dict(d['extraction']['avoid_labels'],p=d['extraction']['avoid_labels']['k']/d['extraction']['avoid_labels']['n'])),
        ('Tasks','all five flags right across runs',rate(tasks['exact'],tasks['paper_runs'],len(VALIDATION['labels']['task']),False)),
        ('Metrics','confirmed value recovered',d['metrics']['in_place']),
        ('Metrics','with pipeline task flags',d['metrics_pipeline_tasks']['in_place']),
        ('Metrics','missed numeric value recovered',d['metrics']['missed_numeric_now_found']),
        ('Pathology check','dataset judged right',rate(sum((r['you']=='yes')==bool(r['pipeline']) for r in pathology),len(pathology))),
        ('Links','link is a real use',rate(tally['p_right'],tally['p_right']+tally['p_parent']+tally['p_wrong'],sum(bool(r['pipeline']) for r in use['rows']),False)),
        ('Links','all uses found',d['dataset_use_recall']['all_uses']),
        ('Links','linkable uses found',d['dataset_use_recall']['linkable_uses']),
        ('Metadata','field right where findable',d['metadata_fields'])]


def stored_rows(s):
    gate=s['gate'];ex=s['extraction'];metric=s['metrics'];links=s['dataset_use']
    return [
        ('Gate','relevant papers kept',rate(**gate['recall'])),
        ('Gate','irrelevant papers rejected',rate(**gate['specificity'])),
        ('Extraction','field matches labeled value',ex['positive_labels']),
        *[('Extraction',field,ex['fields'][field]) for field in ['tissue_site','wsi_or_patch','journal']],
        ('Tasks','all five flags right',rate(s['tasks']['all_five_right'],s['tasks']['papers'])),
        ('Metrics','confirmed value present',metric['value_found']),
        ('Metrics','with partial context',metric['value_found_partial_context']),
        ('Metrics','and task and dataset name',metric['value_found_full_context']),
        ('Metrics','and same stated level',metric['value_found_strict_context']),
        ('Links','labeled use linked',links['recall_with_implied']),
        ('Links','exact dataset only',links['recall_exact'])]


def write_tables(out, result):
    out.mkdir(exist_ok=True)
    for filename,rows in [('table_2_development',development_rows(result['development'])),
                         ('table_3_stored',stored_rows(result['stored']))]:
        with (out/(filename+'.csv')).open('w',newline='',encoding='utf-8') as f:
            writer=csv.DictWriter(f,fieldnames=['step','measure','units','k','n','p','lo','hi'])
            writer.writeheader()
            writer.writerows(dict(step=step,measure=measure,**{k:r[k] for k in ['units','k','n','p','lo','hi'] if k in r}) for step,measure,r in rows)
        lines=[]
        for step,measure,r in rows:
            ci=f" [{r['lo']:.2f}, {r['hi']:.2f}]" if 'lo' in r else ''
            lines.append(f"{step} & {measure} & {r['units']} & {r['k']}/{r['n']} & {r['p']:.2f}{ci} \\\\")
        (out/(filename+'.tex')).write_text('\n'.join(lines)+'\n',encoding='utf-8')
    s=result['screening'];rows=[]
    for route,r in s['routes'].items():rows.append([route,r['input'],r['kept']])
    rows += [['papers retained','',s['papers']],
        *[[f'judged on {kind}','',n] for kind,n in s['text_source'].items()],
        ['papers with task','',s['papers_with_task']],
        ['metric rows',s['metric_rows_generated'],s['metric_rows_on_hub']],
        ['dataset name deduplication',s['dataset_names'],s['canonical_datasets']],
        ['pathology check',s['pathology_input'],s['pathology_kept']],
        ['catalog',s['pathology_kept'],s['catalog_records']],
        *[[f'dataset outcome: {kind}','',n] for kind,n in s['dataset_outcomes'].items()],
        ['operational groups',s['catalog_records'],s['operational_groups']],
        ['paper-dataset links','',s['paper_dataset_pairs']]]
    with (out/'table_1_screening.csv').open('w',newline='',encoding='utf-8') as f:
        writer=csv.writer(f);writer.writerow(['step','input','kept']);writer.writerows(rows)
    rows=result['corpus']['top_datasets_table']
    with (out/'table_4_top_datasets.csv').open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
