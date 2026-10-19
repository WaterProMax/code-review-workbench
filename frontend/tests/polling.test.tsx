import {renderHook, act, waitFor} from '@testing-library/react';
import {test, expect, vi} from 'vitest';
import {apiTimeoutMs} from '../src/api/client';
import {useTaskData} from '../src/hooks/useTaskData';
test('resume refresh restarts polling until completion', async()=>{
  let status='waiting_recovery';
  let reads=0;
  vi.stubGlobal('fetch', vi.fn(async(input)=>{
    const url=String(input);
    let body:any=[];
    if(url==='/api/tasks/T-1') {reads++; body={status};}
    else if(url.includes('/events')) body={events:[],next_seq:0};
    return new Response(JSON.stringify(body),{status:200});
  }));
  const {result,unmount}=renderHook(()=>useTaskData('T-1'));
  await waitFor(()=>expect(result.current.detail?.status).toBe('waiting_recovery'));
  status='running';
  act(()=>result.current.refresh());
  await waitFor(()=>expect(result.current.detail?.status).toBe('running'));
  status='completed';
  await act(async()=>{await new Promise(r=>setTimeout(r,1800));});
  expect(reads).toBeGreaterThanOrEqual(3);
  expect(result.current.detail?.status).toBe('completed');
  unmount(); vi.unstubAllGlobals();
});

test('a hung request does not stop later polls', async () => {
  apiTimeoutMs.current = 40;
  let taskReads = 0;
  let hangEvents = true;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url === '/api/tasks/T-1') {
      taskReads += 1;
      return new Response(JSON.stringify({ status: 'running' }), { status: 200 });
    }
    if (url.includes('/events') && hangEvents) {
      hangEvents = false;
      await new Promise((_resolve, reject) => {
        const abort = () => reject(new DOMException('aborted', 'AbortError'));
        if (init?.signal?.aborted) abort();
        else init?.signal?.addEventListener('abort', abort, { once: true });
      });
    }
    return new Response(JSON.stringify(url.includes('/events') ? { events: [], next_seq: 0 } : []), { status: 200 });
  }));
  const { unmount } = renderHook(() => useTaskData('T-1'));
  await waitFor(() => expect(taskReads).toBeGreaterThanOrEqual(2), { timeout: 4000 });
  unmount();
  apiTimeoutMs.current = 10_000;
  vi.unstubAllGlobals();
});
