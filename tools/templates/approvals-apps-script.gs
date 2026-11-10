// Client Content Review: approvals, comments, photo uploads and editor (Edit Desk) backend. Template: set CLIENT names and passcodes.
// Paste this into Extensions > Apps Script on the "CLIENT Content Review (ETC)" Google Sheet.
// Then set PASSCODE below, run setup() once, and Deploy > New deployment > Web app
// (Execute as: Me, Who has access: Anyone). Send Claude the web app URL.

const PASSCODE = "CHANGE-ME"; // the client reviewers passcode
const NOTIFY_EMAIL = "hello@emilytumiscreative.com"; // gets an email when Amy sends a video back
const AMY_PASSCODE = "CHANGE-ME-TOO"; // ETC editor passcode, never given to the client

const SS = SpreadsheetApp.getActiveSpreadsheet();
const DEC_HEAD = ["key","videoId","pieceKey","reviewer_status","reviewer_note","reviewer_name","reviewer_at","deb_status","deb_note","deb_name","deb_at"];
const CMT_HEAD = ["at","videoId","pieceKey","name","role","text"];
const EDIT_HEAD = ["key","videoId","pieceKey","field","path","text","note","name","at"];
const AMY_HEAD = ["videoId","done_at","name","note"];
const PHO_HEAD = ["id","at","videoId","pieceKey","name","role","note","fileName","fileUrl","fileId","thumbId","removed"];
const PHOTO_FOLDER = "CLIENT Content Review - photo uploads";

function sheet_(name, head){
  let s = SS.getSheetByName(name);
  if(!s){ s = SS.insertSheet(name); s.appendRow(head); s.setFrozenRows(1); }
  return s;
}
function setup(){ sheet_("Decisions", DEC_HEAD); sheet_("Comments", CMT_HEAD); sheet_("Photos", PHO_HEAD); sheet_("Edits", EDIT_HEAD); sheet_("AmyStatus", AMY_HEAD); folder_(); }

function out_(obj){
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
function doGet(){ return out_({ok:true, service:"deb-review"}); }

function doPost(e){
  let b = {};
  try{ b = JSON.parse((e.postData && e.postData.contents) || "{}"); }catch(err){ return out_({ok:false, error:"bad_json"}); }
  if(AMY_PASSCODE && b.code === AMY_PASSCODE) return amy_(b);
  if(!PASSCODE || PASSCODE === "CHANGE-ME" || b.code !== PASSCODE) return out_({ok:false, error:"bad_code"});
  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try{
    if(b.action === "load")    return out_({ok:true, decisions: readDecisions_(), comments: readComments_(), photos: readPhotos_()});
    if(b.action === "photo")   { const err = photo_(b); if(err) return out_({ok:false, error: err}); notifyAarp_(b); return out_({ok:true, photos: readPhotos_()}); }
    if(b.action === "photo_remove") { photoRemove_(b); notifyAarp_(b); return out_({ok:true, photos: readPhotos_()}); }
    if(b.action === "decide")  { decide_(b); notifyAarp_(b); return out_({ok:true, decisions: readDecisions_()}); }
    if(b.action === "comment") { comment_(b); notifyAarp_(b); return out_({ok:true, comments: readComments_()}); }
    return out_({ok:false, error:"unknown_action"});
  } finally { lock.releaseLock(); }
}

function clean_(v, n){ return String(v == null ? "" : v).slice(0, n || 2000); }

function readDecisions_(){
  const rows = sheet_("Decisions", DEC_HEAD).getDataRange().getValues().slice(1);
  const out = {};
  rows.forEach(r => {
    if(!r[0]) return;
    const d = {videoId:r[1], pieceKey:r[2]};
    if(r[3]) d.reviewer = {status:r[3], note:r[4], by:r[5], at:Number(r[6])||0};
    if(r[7]) d.deb = {status:r[7], note:r[8], by:r[9], at:Number(r[10])||0};
    out[r[0]] = d;
  });
  return out;
}
function decide_(b){
  const stage = b.stage === "deb" ? "deb" : "reviewer";
  const status = ["approved","changes","clear"].includes(b.status) ? b.status : null;
  if(!status || !b.videoId || !b.pieceKey) return;
  const key = clean_(b.videoId,80) + "--" + clean_(b.pieceKey,80);
  const s = sheet_("Decisions", DEC_HEAD);
  const vals = s.getDataRange().getValues();
  let row = vals.findIndex((r,i) => i > 0 && r[0] === key) + 1;
  if(row === 0){ s.appendRow([key, clean_(b.videoId,80), clean_(b.pieceKey,80)]); row = s.getLastRow(); }
  const col = stage === "reviewer" ? 4 : 8;
  const v = status === "clear" ? ["","","",""] : [status, clean_(b.note), clean_(b.name,80), Date.now()];
  s.getRange(row, col, 1, 4).setValues([v]);
  if(stage === "reviewer" && status === "changes") s.getRange(row, 8, 1, 4).setValues([["","","",""]]);
}
function readComments_(){
  return sheet_("Comments", CMT_HEAD).getDataRange().getValues().slice(1)
    .filter(r => r[1])
    .map(r => ({at:Number(r[0])||0, videoId:r[1], pieceKey:r[2], by:r[3], role:r[4], text:r[5]}));
}
function comment_(b){
  if(!b.text || !b.videoId || !b.pieceKey) return;
  sheet_("Comments", CMT_HEAD).appendRow([Date.now(), clean_(b.videoId,80), clean_(b.pieceKey,80), clean_(b.name,80), clean_(b.role,20), clean_(b.text)]);
}

// ---- photos AARP adds for static posts ----
function folder_(){
  const props = PropertiesService.getScriptProperties();
  const id = props.getProperty("PHOTO_FOLDER_ID");
  if(id){ try{ return DriveApp.getFolderById(id); }catch(e){} }
  const it = DriveApp.getFoldersByName(PHOTO_FOLDER);
  const f = it.hasNext() ? it.next() : DriveApp.createFolder(PHOTO_FOLDER);
  props.setProperty("PHOTO_FOLDER_ID", f.getId());
  return f;
}
function blob_(b64, type, name){ return Utilities.newBlob(Utilities.base64Decode(b64), type, name); }
function photo_(b){
  const okType = ["image/jpeg","image/png","image/webp"];
  if(!b.videoId || !b.pieceKey || !b.data || !b.thumb) return "missing";
  if(okType.indexOf(b.type) < 0) return "bad_type";
  if(String(b.data).length > 20 * 1024 * 1024) return "too_big";
  const stamp = Utilities.formatDate(new Date(), "America/Chicago", "yyyy-MM-dd HHmm");
  const ext = b.type === "image/png" ? "png" : b.type === "image/webp" ? "webp" : "jpg";
  const base = clean_(b.videoId,80) + " " + clean_(b.pieceKey,40) + " " + stamp + " " + clean_(b.name || "AARP",40);
  const f = folder_();
  const file = f.createFile(blob_(b.data, b.type, base + "." + ext));
  file.setDescription("Uploaded on the review site by " + clean_(b.name,80) + ". Original file: " + clean_(b.fileName,200) + (b.note ? ". Note: " + clean_(b.note,500) : ""));
  const th = f.createFile(blob_(b.thumb, "image/jpeg", "thumb " + base + ".jpg"));
  const id = Utilities.getUuid();
  sheet_("Photos", PHO_HEAD).appendRow([id, Date.now(), clean_(b.videoId,80), clean_(b.pieceKey,80), clean_(b.name,80), clean_(b.role,20), clean_(b.note,500), clean_(b.fileName,200), file.getUrl(), file.getId(), th.getId(), ""]);
  return null;
}
function photoRemove_(b){
  const s = sheet_("Photos", PHO_HEAD);
  const vals = s.getDataRange().getValues();
  const row = vals.findIndex((r,i) => i > 0 && r[0] === b.id) + 1;
  if(row > 0) s.getRange(row, 12).setValue(Date.now());
}
function readPhotos_(){
  const cache = CacheService.getScriptCache();
  return sheet_("Photos", PHO_HEAD).getDataRange().getValues().slice(1)
    .filter(r => r[0] && !r[11])
    .map(r => {
      let thumb = cache.get("t_" + r[10]);
      if(!thumb){ try{ thumb = "data:image/jpeg;base64," + Utilities.base64Encode(DriveApp.getFileById(r[10]).getBlob().getBytes()); if(thumb.length < 95000) cache.put("t_" + r[10], thumb, 21600); }catch(e){ thumb = ""; } }
      return {id:r[0], at:Number(r[1])||0, videoId:r[2], pieceKey:r[3], by:r[4], role:r[5], note:r[6], fileName:r[7], thumb:thumb};
    });
}

// ---- Amy (ETC editor): her own passcode, her own tabs; AARP's passcode can't read these ----
function amy_(b){
  const lock = LockService.getScriptLock(); lock.waitLock(10000);
  try{
    if(b.action === "amy_load") return out_({ok:true, edits: readEdits_(), status: readAmy_()});
    if(b.action === "edit"){ edit_(b); return out_({ok:true, edits: readEdits_()}); }
    if(b.action === "amy_done"){ if(b.videoId){ sheet_("AmyStatus", AMY_HEAD).appendRow([clean_(b.videoId,80), Date.now(), clean_(b.name,80), clean_(b.note,2000)]); notifyAmyDone_(b); } return out_({ok:true, status: readAmy_()}); }
    return out_({ok:false, error:"unknown_action"});
  } finally { lock.releaseLock(); }
}
// Email Emily the moment AARP does anything on the review site
function notifyAarp_(b){
  if(!NOTIFY_EMAIL) return;
  try{
    const who = clean_(b.name, 80) || "Someone at AARP";
    const vid = String(b.videoId || ""), m = vid.match(/^v0*(\d+)$/);
    const title = clean_(b.title, 200) || (m ? "Video " + m[1] : vid);
    const piece = clean_(b.pieceLabel, 120) || clean_(b.pieceKey, 80);
    const where = title + (piece ? " / " + piece : "");
    const stage = b.stage === "deb" ? "Deb" : "Reviewer";
    let subject = "", lines = [];
    if(b.action === "decide"){
      const what = b.status === "approved" ? "approved" : b.status === "changes" ? "requested changes on" : "cleared their decision on";
      subject = "AARP: " + who + " " + what + " " + where;
      lines.push(who + " (" + stage + " step) " + what + ": " + where);
      if(b.note) lines.push("", "Note: " + clean_(b.note, 2000));
    } else if(b.action === "comment"){
      subject = "AARP comment from " + who + ": " + where;
      lines.push(who + " commented on " + where + ":", "", clean_(b.text, 2000));
    } else if(b.action === "photo"){
      subject = "AARP photo from " + who + ": " + where;
      lines.push(who + " uploaded a photo for " + where + ". It's saved in the review photos folder in Drive.");
      if(b.note) lines.push("", "Note: " + clean_(b.note, 500));
    } else if(b.action === "photo_remove"){
      subject = "AARP removed a photo";
      lines.push("Someone on the AARP review site removed a photo they had uploaded.");
    } else return;
    lines.push("", "Open the studio and click Sync now to bring it in.");
    MailApp.sendEmail({to: NOTIFY_EMAIL, subject: subject, body: lines.join("\n"), name: "Content Review"});
  }catch(err){}
}

function notifyAmyDone_(b){
  if(!NOTIFY_EMAIL) return;
  try{
    const title = clean_(b.title || b.videoId, 200), who = clean_(b.name, 80) || "Amy";
    const total = Number(b.total) || 0, approved = Number(b.approved) || 0;
    const lines = [
      who + " sent " + title + " back to you. It's ready for your review.",
      "",
      total ? ("Approved: " + approved + " of " + total + " posts" + (approved < total ? " (" + (total - approved) + " still need your eye)" : "")) : "",
      b.note ? ("Note from " + who + ": " + clean_(b.note, 2000)) : "",
      "",
      "Next: open the studio and click Sync now to bring her edits and approvals in, then review and send to AARP."
    ].filter((x, i, a) => x !== "" || (a[i-1] !== "" && i > 0));
    MailApp.sendEmail({to: NOTIFY_EMAIL, subject: "Ready for you: " + title + " (back from " + who + ")", body: lines.join("\n"), name: "Content Review"});
  }catch(err){}
}
function edit_(b){
  if(!b.videoId || !b.pieceKey || !b.field) return;
  const key = [clean_(b.videoId,80), clean_(b.pieceKey,80), clean_(b.field,80)].join("--");
  const s = sheet_("Edits", EDIT_HEAD);
  const vals = s.getDataRange().getValues();
  let row = vals.findIndex((r,i) => i > 0 && r[0] === key) + 1;
  const v = [key, clean_(b.videoId,80), clean_(b.pieceKey,80), clean_(b.field,80), clean_(JSON.stringify(b.path||[]),400), clean_(b.text,6000), clean_(b.note,1000), clean_(b.name,80), Date.now()];
  if(row === 0){ s.appendRow(v); } else { s.getRange(row, 1, 1, v.length).setValues([v]); }
}
function readEdits_(){
  return sheet_("Edits", EDIT_HEAD).getDataRange().getValues().slice(1).filter(r => r[0])
    .map(r => ({videoId:r[1], pieceKey:r[2], field:r[3], text:r[5], note:r[6], by:r[7], at:Number(r[8])||0}));
}
function readAmy_(){
  return sheet_("AmyStatus", AMY_HEAD).getDataRange().getValues().slice(1).filter(r => r[0])
    .map(r => ({videoId:r[0], at:Number(r[1])||0, by:r[2], note:r[3]}));
}
