"""Reproduce the paper's saved-record results without network or model calls."""
import argparse
import json
import math
from pathlib import Path
import socket
import shutil
import subprocess


def compare(actual, expected, path=''):
    if isinstance(expected,dict):
        if not isinstance(actual,dict) or set(actual)!=set(expected):
            raise AssertionError(f'{path}: result keys changed')
        for k,v in expected.items():compare(actual[k],v,path+'/'+str(k))
    elif isinstance(expected,list):
        if not isinstance(actual,list) or len(actual)!=len(expected):
            raise AssertionError(f'{path}: result count changed')
        for i,v in enumerate(expected):compare(actual[i],v,path+'/'+str(i))
    elif isinstance(expected,(int,float)) and not isinstance(expected,bool):
        if math.isnan(expected):assert math.isnan(actual),path
        else:assert math.isclose(actual,expected,rel_tol=1e-12,abs_tol=1e-12),f'{path}: {actual} != {expected}'
    else:
        assert actual==expected,f'{path}: {actual!r} != {expected!r}'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,default=Path('results'))
    args=parser.parse_args()
    def no_network(*args,**kwargs):
        raise RuntimeError('Paper reproduction does not make network calls')
    socket.socket.connect=no_network
    from evaluation import analyze,validate,tables
    from evaluation.common import DATA
    args.out.mkdir(parents=True,exist_ok=True)
    print('Reproducing corpus statistics and figures...',flush=True)
    corpus=analyze.run(out_dir=args.out)
    print('Reproducing development and stored-record validation...',flush=True)
    result={'corpus':corpus,'development':validate.run_development(),'stored':validate.run_stored()}
    # Compare against the paper outputs saved before the repository cleanup.
    expected=json.loads((DATA/'expected.json').read_text(encoding='utf-8'))
    compare(json.loads(json.dumps(result)),expected)
    result['screening']=tables.screening(corpus)
    result['operations']=tables.operations()
    tables.write_tables(args.out/'tables',result)
    compiler=shutil.which('pdflatex')
    diagram=Path(__file__).parent/'evaluation/fig_pipeline.tex'
    if compiler:
        build=subprocess.run([compiler,'-interaction=nonstopmode','-halt-on-error',
            '-output-directory',str((args.out/'figures').resolve()),str(diagram.resolve())],
            capture_output=True,text=True,encoding='utf-8',errors='replace')
        if build.returncode:
            raise RuntimeError('Figure 1 compilation failed:\n'+build.stdout[-2000:])
        for suffix in ['.aux','.log']:
            (args.out/'figures'/('fig_pipeline'+suffix)).unlink(missing_ok=True)
    else:
        print('Figure 1: install pdflatex to compile evaluation/fig_pipeline.tex.')
    (args.out/'results.json').write_text(json.dumps(result,indent=1)+'\n',encoding='utf-8')
    print(f'All paper statistics match. Figures, tables and results: {args.out.resolve()}')


if __name__=='__main__':
    main()
