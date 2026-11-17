// Bound to a NEW, dedicated Google spreadsheet. Run setupPropertySheet once.
const HEADERS = ['Owner','Property','Address','Occupancy','Check-in / next arrival','Checkout',
  'Occupied days','Days remaining','Lock status','Lowest lock battery %','Thermostat readings',
  'Needs attention','Issues','Lock read at','Thermostat seen at','Hostaway sync at','Snapshot at'];

function output(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}
function hex(bytes) {
  return bytes.map(b => ('0' + ((b + 256) % 256).toString(16)).slice(-2)).join('');
}
function authenticate(body, key, now) {
  if (!key || key.length < 32 || !body || typeof body.data !== 'string' || body.data.length > 2000000 ||
      !/^\d{10,12}$/.test(body.timestamp) || Math.abs(now - Number(body.timestamp)) > 300 ||
      !/^[a-f0-9]{64}$/.test(body.signature)) throw new Error('Invalid request');
  const expected = hex(Utilities.computeHmacSha256Signature(body.timestamp + '\n' + body.data, key, Utilities.Charset.UTF_8));
  let difference = 0;
  for (let i = 0; i < expected.length; i++) difference |= expected.charCodeAt(i) ^ body.signature.charCodeAt(i);
  if (difference) throw new Error('Invalid signature');
  const snapshot = JSON.parse(body.data);
  if (snapshot.schema !== 1 || JSON.stringify(snapshot.headers) !== JSON.stringify(HEADERS) ||
      !Array.isArray(snapshot.rows) || snapshot.rows.length < 1 || snapshot.rows.length > 1000 ||
      snapshot.rows.some(row => !Array.isArray(row) || row.length !== HEADERS.length ||
        row.some(v => !(typeof v === 'string' && v.length <= 4001) && !(typeof v === 'number' && Number.isFinite(v))))) {
    throw new Error('Invalid snapshot');
  }
  return snapshot;
}
function literal(value) {
  return typeof value === 'string' && /^[\s]*[=+@-]/.test(value) ? "'" + value : value;
}

function setupPropertySheet() {
  const props = PropertiesService.getScriptProperties();
  if (props.getProperty('SHEET_ID')) throw new Error('Already set up. Keep the existing connection key.');
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  if (ss.getSheetByName('Property status')) throw new Error('Use a new spreadsheet; Property status already exists.');
  const sheet = ss.insertSheet('Property status');
  sheet.getRange(1, 1, 1, HEADERS.length).setValues([HEADERS]);
  sheet.setFrozenRows(1).setFrozenColumns(3);
  sheet.getRange(1, 1, 1, HEADERS.length).setBackground('#163c40').setFontColor('#ffffff').setFontWeight('bold');
  sheet.setColumnWidths(1, HEADERS.length, 150);
  sheet.setColumnWidth(2, 220).setColumnWidth(3, 260).setColumnWidth(11, 340).setColumnWidth(13, 420);
  sheet.getRange(2, 1, sheet.getMaxRows()-1, HEADERS.length).setWrap(true);
  const range = {sheetId: sheet.getSheetId(), startRowIndex: 0, startColumnIndex: 0, endColumnIndex: HEADERS.length};
  const views = [
    ['All homes', null], ['Mike', '=\u0024A2="Mike"'], ['Kurt', '=\u0024A2="Kurt"'], ['Needs attention', '=\u0024L2="Yes"']
  ];
  const requests = views.map(([title, formula]) => ({addFilterView: {filter: {
    title, range, ...(formula ? {criteria: {'0': {condition: {type: 'CUSTOM_FORMULA', values: [{userEnteredValue: formula}]}}}} : {})
  }}}));
  // Enable the Google Sheets API under Services in the Apps Script editor before setup.
  let result;
  try {
    result = Sheets.Spreadsheets.batchUpdate({requests}, ss.getId());
  } catch (error) {
    ss.deleteSheet(sheet); // No app data has been written; allow setup again after enabling the service.
    throw new Error('Enable the Google Sheets API in Services, then run setupPropertySheet again.');
  }
  const key = Utilities.getUuid().replace(/-/g, '') + Utilities.getUuid().replace(/-/g, '');
  props.setProperties({SHEET_ID: ss.getId(), TAB_ID: String(sheet.getSheetId()), CONNECTION_KEY: key});
  const links = result.replies.map((reply, i) => views[i][0] + ': ' + ss.getUrl() + '#gid=' + sheet.getSheetId() + '&fvid=' + reply.addFilterView.filter.filterViewId).join('\n');
  // The user can copy the key from Project Settings → Script properties. It is never stored in a cell.
  SpreadsheetApp.getUi().alert('Ready. Copy CONNECTION_KEY from Project Settings → Script properties.\n\nSaved views:\n' + links);
}

function doGet() { return output({ok: false, message: 'Signed app snapshots only. Open the Google spreadsheet to view properties.'}); }

function doPost(event) {
  const lock = LockService.getScriptLock();
  let locked = false;
  try {
    if (!event || !event.postData || event.postData.contents.length > 2100000) throw new Error('Invalid request');
    const body = JSON.parse(event.postData.contents);
    const props = PropertiesService.getScriptProperties();
    const snapshot = authenticate(body, props.getProperty('CONNECTION_KEY'), Math.floor(Date.now()/1000));
    const digest = hex(Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, body.data, Utilities.Charset.UTF_8));
    lock.waitLock(10000); locked = true;
    const previous = Number(props.getProperty('LAST_TIMESTAMP') || 0);
    const lastDigest = props.getProperty('LAST_DIGEST');
    if (Number(body.timestamp) < previous || (Number(body.timestamp) === previous && digest !== lastDigest)) throw new Error('Older snapshot');
    if (digest === lastDigest) return output({ok: true, digest, rows: snapshot.rows.length});
    const ss = SpreadsheetApp.openById(props.getProperty('SHEET_ID'));
    const sheet = ss.getSheets().find(s => String(s.getSheetId()) === props.getProperty('TAB_ID'));
    if (!sheet) throw new Error('Missing sheet');
    const values = snapshot.rows.map(row => row.map(literal));
    const previousRows = sheet.getLastRow();
    if (sheet.getMaxRows() < values.length + 1) sheet.insertRowsAfter(sheet.getMaxRows(), values.length + 1 - sheet.getMaxRows());
    // A single rectangular write replaces the snapshot; replay simply replaces the same rows.
    sheet.getRange(2, 1, values.length, HEADERS.length).setValues(values).setWrap(true);
    sheet.getRange(2, 1, values.length, HEADERS.length).setBackgrounds(values.map(row => Array(HEADERS.length).fill(row[11] === 'Yes' ? '#fff1e8' : '#ffffff')));
    if (previousRows > values.length + 1) sheet.getRange(values.length + 2, 1, previousRows - values.length - 1, HEADERS.length).clearContent().setBackground('#ffffff');
    SpreadsheetApp.flush();
    props.setProperties({LAST_TIMESTAMP: body.timestamp, LAST_DIGEST: digest});
    return output({ok: true, digest, rows: values.length});
  } catch (error) {
    return output({ok: false, message: 'Update rejected or incomplete. Check setup and connection key.'});
  } finally {
    if (locked) lock.releaseLock();
  }
}
