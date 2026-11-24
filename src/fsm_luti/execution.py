"""Bounded project-process execution with parent-owned progress and reports."""
from concurrent.futures import ProcessPoolExecutor, wait, FIRST_COMPLETED
from contextlib import contextmanager
from collections.abc import Mapping
from numbers import Integral
import multiprocessing as mp
import os
import math
from queue import Empty


def validate_execution_controls(options):
    if not isinstance(options, Mapping):
        raise ValueError('execution must be a dictionary')
    unknown=set(options)-{'case_workers','native_threads','memory_per_case_gb','memory_reserve_gb'}
    if unknown:raise ValueError(f'Unknown execution controls: {sorted(unknown)}')
    for name in ('case_workers','native_threads'):
        value=options.get(name,1)
        if isinstance(value,bool) or not isinstance(value,Integral) or value<1:
            raise ValueError(f'execution.{name} must be a positive integer')
    for name,default in [('memory_per_case_gb',12.),('memory_reserve_gb',4.)]:
        value=options.get(name,default)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0 or (name=='memory_per_case_gb' and value==0):
            raise ValueError(f'execution.{name} must be finite and '+('positive' if name=='memory_per_case_gb' else 'nonnegative'))


def available_memory_gb():
    """Available physical RAM in GiB, or None when the OS cannot report it."""
    try:
        if os.name=='nt':
            import ctypes
            class MemoryStatus(ctypes.Structure):
                _fields_=[('length',ctypes.c_ulong),('load',ctypes.c_ulong),
                    *[(name,ctypes.c_ulonglong) for name in ('total_phys','avail_phys','total_page','avail_page','total_virtual','avail_virtual','avail_extended')]]
            status=MemoryStatus();status.length=ctypes.sizeof(status)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):return None
            return status.avail_phys/2**30
        return os.sysconf('SC_AVPHYS_PAGES')*os.sysconf('SC_PAGE_SIZE')/2**30
    except (AttributeError,OSError,ValueError):return None


def plan_case_workers(config,case_count):
    options=config.get('execution',{});validate_execution_controls(options)
    requested=options.get('case_workers',1)
    available=available_memory_gb()
    per_case=options.get('memory_per_case_gb',12.)
    reserve=options.get('memory_reserve_gb',4.)
    cpu_count=(getattr(os,'process_cpu_count',os.cpu_count)() or 1)
    assignment=config.get('transport',{}).get('assignment',{})
    loading_workers=assignment.get('loading_workers',1)
    # A project parent computes mode choice/LUTI while its routing children work.
    cpu_per_case=(1+loading_workers if loading_workers>1 else 1)*options.get('native_threads',1)
    cpu_limit=max(1,cpu_count//cpu_per_case)
    memory_limit=1 if available is None else max(1,int((available-reserve)//per_case))
    effective=max(1,min(requested,max(1,case_count),cpu_limit,memory_limit))
    return {'requested_case_workers':requested,'effective_case_workers':effective,
        'available_memory_gb':available,'memory_per_case_gb':per_case,'memory_reserve_gb':reserve,
        'logical_cpus':cpu_count,'cpu_limit':cpu_limit,'memory_limit':memory_limit,
        'assignment_workers_per_case':loading_workers,'native_threads':options.get('native_threads',1),
        'memory_guard_is_estimate':True}


@contextmanager
def child_thread_environment(threads):
    names=('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS')
    previous={key:os.environ.get(key) for key in names}
    try:
        for key in names:os.environ[key]=str(threads)
        yield
    finally:
        for key,value in previous.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value


def run_case_pool(tasks,*,workers,initializer,initargs,worker,progress,native_threads=1):
    """Yield small case results; large transport arrays stay in their worker.

    The initializer receives (*initargs, queue, stop_event). Child processes
    never write the parent's event log or terminal. On failure, stop siblings
    at their next progress checkpoint before releasing the reference cache.
    """
    context=mp.get_context('spawn')
    queue=context.Queue();stop=context.Event()
    executor=None;pending=set()
    def drain():
        while True:
            try:progress(queue.get_nowait())
            except Empty:break
    try:
        with child_thread_environment(native_threads):
            executor=ProcessPoolExecutor(max_workers=workers,mp_context=context,
                initializer=initializer,initargs=(*initargs,queue,stop))
            pending={executor.submit(worker,task) for task in tasks}
        while pending:
            done,pending=wait(pending,timeout=.25,return_when=FIRST_COMPLETED)
            drain()
            for future in done:yield future.result()
        executor.shutdown(wait=True);executor=None
        drain()
    except BaseException:
        stop.set()
        for future in pending:future.cancel()
        # Keep consuming events while workers unwind, so a full pipe cannot
        # deadlock Queue's feeder thread during process shutdown.
        while pending:
            _,pending=wait(pending,timeout=.25)
            drain()
        raise
    finally:
        if executor is not None:executor.shutdown(wait=True,cancel_futures=True)
        drain();queue.close();queue.join_thread()
