// MaxSeat mailer: lets the website send account emails from your own Gmail.
// Gmail blocks SMTP logins from Vercel's servers, so the site calls this script
// over normal HTTPS instead. Free Gmail accounts can email 100 people a day.
//
// Setup (about 5 minutes):
//  1. Go to https://script.google.com and click New project.
//     Delete what is in the editor and paste this whole file. Save.
//  2. Change SECRET below to your own secret. Put the same value in Vercel
//     as GOOGLE_SCRIPT_SECRET.
//  3. Pick "testSend" in the function list at the top and click Run.
//     Allow access when Google asks. You should get a test email.
//  4. Click Deploy > New deployment > the gear icon > Web app.
//     Execute as: Me.  Who has access: Anyone.  Click Deploy.
//  5. Copy the Web app URL (it ends in /exec). Put it in Vercel as
//     GOOGLE_SCRIPT_URL, then redeploy the site.
//
// If you edit this script later: Deploy > Manage deployments > pencil icon >
// Version: New version > Deploy. The URL stays the same.

var SECRET = 'CHANGE-ME';

function doPost(e) {
  try {
    var d = JSON.parse(e.postData.contents);
    if (d.secret !== SECRET) {
      return reply({ ok: false, error: 'Wrong secret. GOOGLE_SCRIPT_SECRET in Vercel must match SECRET in the script.' });
    }
    if (!d.to) return reply({ ok: false, error: 'No email address given.' });
    var opts = {
      to: d.to,
      subject: d.subject || 'MaxSeat Alert',
      body: d.text || '',
      name: d.name || 'MaxSeat Alert System'
    };
    if (d.html) opts.htmlBody = d.html;
    MailApp.sendEmail(opts);
    return reply({ ok: true });
  } catch (err) {
    return reply({ ok: false, error: String(err) });
  }
}

// Open the /exec URL in a browser to check the script is up.
function doGet() {
  return reply({ ok: true, message: 'MaxSeat mailer is running. Emails left today: ' + MailApp.getRemainingDailyQuota() });
}

// Run this once from the editor to allow access and get a test email.
function testSend() {
  MailApp.sendEmail(Session.getEffectiveUser().getEmail(), 'MaxSeat mailer test', 'It works. The website can now send account emails.');
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
