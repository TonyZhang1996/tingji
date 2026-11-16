#ifndef AppVersion
  #error AppVersion must be provided by the build script
#endif
#ifndef BundleDir
  #error BundleDir must be provided by the build script
#endif
#ifndef VendorDir
  #error VendorDir must be provided by the build script
#endif
#ifndef ReleaseDir
  #error ReleaseDir must be provided by the build script
#endif

[Setup]
AppId={{B45F178A-2438-4E5D-93FD-12EB5510E9A1}
AppName=听记
AppVersion={#AppVersion}
AppVerName=听记 {#AppVersion}
AppPublisher=TonyZhang1996
AppPublisherURL=https://github.com/TonyZhang1996/tingji
AppSupportURL=https://github.com/TonyZhang1996/tingji/issues
AppUpdatesURL=https://github.com/TonyZhang1996/tingji/releases
DefaultDirName={localappdata}\Programs\Tingji
DefaultGroupName=听记
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64os
ArchitecturesInstallIn64BitMode=x64os
MinVersion=10.0.17763
OutputDir={#ReleaseDir}
OutputBaseFilename=Tingji-Setup-{#AppVersion}-windows-x64
SetupIconFile={#BundleDir}\_internal\assets\tingji.ico
UninstallDisplayIcon={app}\Tingji.exe
WizardStyle=modern
Compression=lzma2/ultra64
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
SignedUninstaller=no
Uninstallable=yes
ChangesAssociations=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式 / Create a desktop shortcut"; GroupDescription: "快捷方式 / Shortcuts:"; Flags: unchecked

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#VendorDir}\prerequisites\MicrosoftEdgeWebView2RuntimeInstallerX64.exe"; Flags: dontcopy

[Icons]
Name: "{group}\听记"; Filename: "{app}\Tingji.exe"; WorkingDir: "{app}"
Name: "{group}\卸载听记"; Filename: "{uninstallexe}"
Name: "{autodesktop}\听记"; Filename: "{app}\Tingji.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\Tingji.exe"; Description: "启动听记 / Launch Tingji"; Flags: nowait postinstall skipifsilent

[Code]
const
  WebViewClientKey = 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

function HasWebViewAt(RootKey: Integer): Boolean;
var
  Version: String;
begin
  Result := RegQueryStringValue(RootKey, WebViewClientKey, 'pv', Version) and
            (Version <> '') and (Version <> '0.0.0.0');
end;

function NeedWebView2: Boolean;
begin
  Result := not (HasWebViewAt(HKCU) or HasWebViewAt(HKLM32) or HasWebViewAt(HKLM64));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ExitCode: Integer;
begin
  Result := '';
  if NeedWebView2 then begin
    ExtractTemporaryFile('MicrosoftEdgeWebView2RuntimeInstallerX64.exe');
    if not Exec(ExpandConstant('{tmp}\MicrosoftEdgeWebView2RuntimeInstallerX64.exe'),
                '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ExitCode) then begin
      Result := 'WebView2 安装程序未能启动。/ Unable to start the WebView2 installer.';
      Exit;
    end;
    if ExitCode = 3010 then NeedsRestart := True;
    if NeedWebView2 then
      Result := 'WebView2 安装未完成，请重试。/ WebView2 installation did not complete. Exit code: ' + IntToStr(ExitCode);
  end;
end;

// No [UninstallDelete] rule targets the user's data directory.
