import asyncio
import httpx
import pytest
from fastapi.testclient import TestClient
from app.ha import HAClient
from app.web import create_app, dashboard_rows
from test_sync import syncer, run


@pytest.mark.parametrize('state,offline', [('locked',False),('unlocked',False),('unavailable',True),('unknown',True),(None,True)])
def test_check_lock_refreshes_only_target_availability(syncer,state,offline):
    run(syncer.import_listings()); run(syncer.discover_locks())
    lock=syncer.db.one("SELECT * FROM locks WHERE entity_id='lock.124_maple'")
    syncer.db.execute("UPDATE locks SET state='unavailable',fail_count=2,last_error='keep verification failure' WHERE id=?",(lock['id'],))
    other=syncer.db.one("SELECT * FROM locks WHERE entity_id='lock.cc6_schlage'")
    calls=[]
    async def read(eid):
        calls.append(eid)
        return state
    syncer.ha.lock_state=read
    response=TestClient(create_app(syncer)).post('/lock-refresh',data={'property_id':lock['property_id']},follow_redirects=False)
    assert response.status_code==303
    assert calls==[lock['entity_id']]
    assert syncer.db.one("SELECT id FROM events WHERE kind='lock.checked' AND property_id=?",
                         (lock['property_id'],))
    updated=syncer.db.one('SELECT * FROM locks WHERE id=?',(lock['id'],))
    assert (updated['state']=='unavailable') is offline
    assert updated['fail_count']==2 and updated['last_error']=='keep verification failure'
    assert syncer.db.one('SELECT * FROM locks WHERE id=?',(other['id'],))==other
    board=next(r for r in dashboard_rows(syncer) if r['id']==lock['property_id'])
    assert any(p['text'].endswith(' offline') for p in board['problems']) is offline


def test_failed_state_read_does_not_clear_offline_after_successful_code_read(syncer):
    run(syncer.import_listings()); run(syncer.discover_locks())
    syncer.db.execute("UPDATE locks SET state='unavailable'")
    lock=syncer.db.one("SELECT * FROM locks WHERE entity_id='lock.124_maple'")
    async def fail(eid):
        raise RuntimeError('unreachable')
    syncer.ha.lock_state=fail
    c=TestClient(create_app(syncer))
    result=c.post('/lock-refresh',data={'property_id':lock['property_id']},follow_redirects=False)
    assert 'could+not+be+confirmed' in result.headers['location']
    updated=syncer.db.one('SELECT * FROM locks WHERE id=?',(lock['id'],))
    assert updated['state']=='unavailable' and updated['codes_read_at']


@pytest.mark.parametrize('status,data,expected',[(200,{'entity_id':'lock.test','state':'locked'},'locked'),(404,{},None)])
def test_targeted_ha_read_is_get_only(status,data,expected):
    calls=[]
    def handle(request):
        calls.append((request.method,request.url.path))
        return httpx.Response(status,json=data)
    ha=HAClient('http://ha','t',http=httpx.AsyncClient(base_url='http://ha',transport=httpx.MockTransport(handle)))
    assert run(ha.lock_state('lock.test'))==expected
    assert calls==[('GET','/api/states/lock.test')]
