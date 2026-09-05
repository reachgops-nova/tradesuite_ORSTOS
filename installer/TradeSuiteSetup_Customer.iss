; ==========================================================
; TradeSuite
; installer/TradeSuiteSetup_Customer.iss
; ==========================================================
;
; The CUSTOMER-edition installer -- wraps dist_customer\TradeSuite.exe
; (built with engine/edition.py set to "customer" via
; installer/set_edition.py, into an ISOLATED --distpath/--workpath so it
; can never overwrite dist\TradeSuite.exe, which is Gopinath's own
; internal build). Everything else -- AppId, upgrade-preservation rules,
; per-user install location -- mirrors TradeSuiteSetup.iss exactly; see
; that file's header for the reasoning. This is a separate .iss only
; because it points at a separate source exe and a separate output file,
; not because the product itself is different.
;
; Build (after producing dist_customer\TradeSuite.exe -- see
; installer/set_edition.py's own docstring for the two-command sequence
; that builds it without disturbing the internal build):
;     iscc TradeSuiteSetup_Customer.iss
; Output: Output\Customer\TradeSuiteSetup.exe
;
; Same AppId as the internal installer is fine here: this ships to a
; DIFFERENT machine entirely (a customer's, never Gopinath's own), so
; there is no risk of the two editions colliding as "the same install."
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
CloseApplications=yes
RestartApplications=yes
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=Output\Customer
; Gopinath, 2026-08-31: name the customer deliverable TSC_0926 -- TSC
; ("TradeSuite Client") plus a build/drop date stamp, so successive drops
; sent to customers are distinguishable by filename alone.
OutputBaseFilename=TSC_0926
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
; dist_customer\, NOT dist\ -- see this file's header. Double-check this
; path if it's ever edited; the internal installer's own header explains
; exactly how a wrong-folder mistake here can silently ship a stale or
; wrong-edition binary while every install/launch check still passes.
Source: "dist_customer\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName} now"; Flags: nowait postinstall skipifsilent

; OpenAlgo itself is not bundled -- TradeSuite's own first launch fetches
; a fresh, pinned copy via HTTPS zip + a private embeddable Python runtime
; (installer/openalgo_bootstrap.py, rewritten 2026-08-24) -- no git, no
; system Python required on the customer's machine. Internet access is
; the one real prerequisite (GitHub + python.org + PyPI reachable).
