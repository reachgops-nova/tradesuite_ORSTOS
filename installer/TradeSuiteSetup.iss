; ==========================================================
; TradeSuite
; installer/TradeSuiteSetup.iss
; ==========================================================
;
; Wraps dist\TradeSuite.exe (built by TradeSuite.spec) into a real Windows
; installer -- SureFramePro never needed this (no external dependency, a
; bare PyInstaller onefile exe was enough), but TradeSuite does: it has a
; genuine separate runtime dependency (OpenAlgo, fetched by
; engine/openalgo_manager.py on first run) that a raw exe hand-off can't
; cover on its own, and Program Files is the correct install location for
; a real product, unlike "just run it from wherever."
;
; The UAC prompt this shows during install is a normal, expected part of
; installing into Program Files -- the person installing it explicitly
; consents via Windows' own dialog. Not the same thing as a background
; process silently self-elevating without anyone seeing a prompt.
;
; Build (after `pyinstaller TradeSuite.spec` has produced dist\TradeSuite.exe):
;     iscc TradeSuiteSetup.iss
; Output: Output\TradeSuiteSetup.exe
;
; TODO before shipping: add an icon/publisher URL once branding exists.
;
; ## Upgrades must preserve the customer's install (Gopinath, 2026-08-25)
;
; Two things make "install the new version over the old one" an UPGRADE
; rather than a second parallel install, and both are load-bearing:
;
;  1. AppId below must NEVER change again. Inno matches an existing
;     install by AppId alone -- change it and the new version installs
;     alongside the old one, leaving two Start Menu entries, two
;     uninstallers, and a customer running whichever they happen to
;     click. (The previous value here wasn't a valid GUID; fixed once,
;     now frozen.)
;  2. Nothing in [Files] or a [UninstallDelete] may ever point at
;     %LOCALAPPDATA%\TradeSuite. That directory -- settings, licence,
;     trade history, activity logs, the fetched OpenAlgo runtime -- is
;     the customer's, and the installer must not touch it in either
;     direction. Only {app} (the program directory) is ours to replace.
;
; That combination is what lets a credential/feature update ship as a
; plain reinstall: broker connection, licence expiry and full trade
; history all survive untouched.
; ==========================================================

#define MyAppName "TradeSuite"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "Gopinath"
#define MyAppExeName "TradeSuite.exe"

[Setup]
AppId={{B6F1B0F3-6E39-4B7E-9C4A-7D2E5A9C4001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
; An upgrade over a running copy would otherwise fail to replace the exe
; (Windows holds it open). Prompt to close it, and restart it afterwards.
CloseApplications=yes
RestartApplications=yes
; Per-user install, no admin elevation required -- deliberate, not just a
; convenience: a customer running this on their own machine may not have
; admin rights either, and TradeSuite already treats %LOCALAPPDATA% as its
; home for data (see engine/config_store.py's _install_dir()), so putting
; the program itself there too keeps everything under one no-elevation-
; needed root instead of splitting across Program Files + LocalAppData.
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=Output
OutputBaseFilename=TradeSuiteSetup
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
DisableProgramGroupPage=yes
WizardStyle=modern

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; Relative to THIS .iss file, i.e. installer\dist\ -- which is where
; PyInstaller actually writes, because the documented build command runs
; `pyinstaller TradeSuite.spec` from inside installer\.
;
; This previously read "..\dist\" (= TradeSuiteApp\dist\), a stale folder
; left over from a build once run from the project root. The compile
; succeeded, the install succeeded, and the app launched fine -- it was
; just an OLD BINARY every time. Nothing about installing or launching
; reveals that; only checking the installed exe against the one just
; built does. If this ever needs to change, verify the installed file's
; size/timestamp against dist\ afterwards, not just that setup ran.
Source: "dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: nowait postinstall skipifsilent

; NOTE: this installer does NOT bundle or install OpenAlgo -- see the
; plan's licensing rationale. TradeSuite's first launch (via
; engine/openalgo_manager.py, driven from gui/setup_wizard.py) fetches a
; fresh, pinned copy via HTTPS zip download + a private embeddable Python
; runtime it sets up for itself (installer/openalgo_bootstrap.py,
; rewritten 2026-08-24) -- genuinely standalone: no git, no system Python
; needed on the customer's machine. The one real prerequisite is internet
; access (GitHub + python.org + PyPI reachable) at first-run time.
