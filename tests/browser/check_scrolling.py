from pathlib import Path
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[2]
static = root / 'stay_automation' / 'app' / 'static'
css = (static / 'style.css').read_text()
js = (static / 'scrolling.js').read_text()
rows = ''.join(f'<tr><td><input name="name_{i}" value="Home {i}"></td><td>Front door and thermostat</td><td><input type="checkbox" name="auto_{i}"></td><td>Active</td></tr>' for i in range(50))
html = f'''<!doctype html><html><head><meta name="viewport" content="width=device-width, initial-scale=1"><style>{css}</style></head><body>
<header><h1>Stays test</h1></header><main><form method="post" action="properties-save" id="save-properties" data-save-label="Save properties">
<section class="panel"><h2>Homes</h2><div class="scroll"><table class="homes-table" style="min-width:950px"><thead><tr><th>Home</th><th>Devices</th><th>Lock auto</th><th>Active</th></tr></thead><tbody>{rows}</tbody></table></div><button>Save</button></section></form>
<form method="post" action="recipients-save" id="save-recipients" data-save-label="Save recipients"><input name="rname_new" value="Kurt" required></form><div style="height:900px">After the table</div></main>
<script>window.submissions=[];document.addEventListener('submit',e=>{{e.preventDefault();window.submissions.push({{id:e.target.id,values:Array.from(new FormData(e.target).entries())}})}});</script><script>{js}</script></body></html>'''

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    for width,height in [(1280,800),(390,844)]:
        page=browser.new_page(viewport={'width':width,'height':height})
        page.set_content(html)
        page.wait_for_timeout(100)
        assert not page.locator('.floating-columns').is_visible()
        bar=page.locator('.save-bar').bounding_box()
        assert abs(bar['y']+bar['height']-height)<1
        page.evaluate('window.scrollTo(0,700)')
        page.wait_for_timeout(100)
        assert page.locator('.floating-columns').is_visible()
        assert abs(page.locator('.floating-columns').bounding_box()['y'])<1
        page.evaluate("document.querySelector('.scroll').scrollLeft=220")
        page.wait_for_timeout(100)
        source=page.locator('main thead th').nth(1).bounding_box()
        floating=page.locator('.floating-columns th').nth(1).bounding_box()
        assert abs(source['x']-floating['x'])<2, (source,floating)
        assert abs(source['width']-floating['width'])<2
        page.locator('input[name=name_25]').fill('Edited home')
        page.wait_for_timeout(50)
        assert page.locator('.save-bar').get_by_text('Unsaved changes').is_visible()
        page.get_by_role('button',name='Save properties',exact=True).click()
        assert page.evaluate('window.submissions[0].id')=='save-properties'
        assert page.evaluate("window.submissions[0].values.some(x=>x[0]==='name_25'&&x[1]==='Edited home')")
        page.get_by_role('button',name='Save recipients',exact=True).click()
        assert page.evaluate('window.submissions[1].id')=='save-recipients'
        page.evaluate('window.scrollTo(0,document.body.scrollHeight)')
        page.wait_for_timeout(100)
        assert not page.locator('.floating-columns').is_visible()
        assert abs(page.locator('.save-bar').bounding_box()['y']+page.locator('.save-bar').bounding_box()['height']-height)<1
        assert page.evaluate("window.submissions[0].values.every(x=>x[0]!=='rname_new')")
        page.locator('input[name=rname_new]').fill('')
        page.get_by_role('button',name='Save recipients',exact=True).click()
        assert page.evaluate('window.submissions.length') == 2  # native required-field validation
        page.emulate_media(media='print')
        assert not page.locator('.save-bar').is_visible()
        assert not page.locator('.floating-columns').is_visible()
        page.emulate_media(media='screen')
        page.close()
    browser.close()
print('PASS: desktop and phone headers, horizontal alignment, fixed Save bar, unsaved indicators, native form submissions, table-end cleanup')
