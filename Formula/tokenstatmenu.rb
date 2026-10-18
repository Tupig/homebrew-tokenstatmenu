# release-pin: {"build":"7","commit":"d4ee8a1080b6a4d54ff1cbc0594aac924798a3ac","source_bytes":2136023,"source_name":"TokenStatMenu-1.1.5-source.zip","source_sha256":"1108c7021905f5222e8f638b1294bdddfaa364eb87bf4be5e36353ea1ceaf8c9","tag":"v1.1.5","version":"1.1.5"}
require "shellwords"

class Tokenstatmenu < Formula
  desc "Native menu bar usage statistics for Seasun Token Hub"
  homepage "https://github.com/Tupig/TokenStatMenu"
  url "https://github.com/Tupig/TokenStatMenu/releases/download/v1.1.5/TokenStatMenu-1.1.5-source.zip"
  sha256 "1108c7021905f5222e8f638b1294bdddfaa364eb87bf4be5e36353ea1ceaf8c9"
  license "MIT"

  depends_on xcode: ["15.0", :build]
  depends_on macos: :ventura

  def install
    ENV["DEVELOPER_DIR"] = MacOS::Xcode.prefix.to_s
    architecture = Hardware::CPU.arm? ? "arm64" : "x86_64"
    xcodebuild "-project", "TokenStatMenu.xcodeproj", "-scheme", "TokenStatMenu",
               "-configuration", "Release", "-destination", "generic/platform=macOS",
               "-derivedDataPath", buildpath/"brew-derived",
               "ARCHS=#{architecture}", "ONLY_ACTIVE_ARCH=YES",
               "GENERATE_PKGINFO_FILE=NO", "CODE_SIGNING_ALLOWED=NO", "CODE_SIGNING_REQUIRED=NO", "build"
    built = buildpath/"brew-derived/Build/Products/Release/TokenStatMenu.app"
    info = built/"Contents/Info.plist"
    plist = "/usr/libexec/PlistBuddy"
    built_version = Utils.safe_popen_read(plist, "-c", "Print :CFBundleShortVersionString", info).strip
    built_build = Utils.safe_popen_read(plist, "-c", "Print :CFBundleVersion", info).strip
    odie "Release version mismatch" if built_version != version.to_s
    odie "Release build mismatch" if built_build != "7"
    libexec.install built
    app = libexec/"TokenStatMenu.app"
    system "/usr/bin/plutil", "-replace", "TokenStatSourceRevision", "-string",
           "d4ee8a1080b6a4d54ff1cbc0594aac924798a3ac", app/"Contents/Info.plist"
    system "/usr/bin/plutil", "-replace", "TokenStatSourceState", "-string",
           "release-source", app/"Contents/Info.plist"
    system "/usr/bin/codesign", "--force", "--sign", "-", app
    system "/usr/bin/codesign", "--verify", "--deep", "--strict", app
    app_path = (opt_libexec/"TokenStatMenu.app").to_s.shellescape
    (bin/"tokenstatmenu").write <<~SH
      #!/bin/sh
      if [ "$#" -ne 0 ]; then
        echo "Usage: tokenstatmenu" >&2
        exit 64
      fi
      exec /usr/bin/open #{app_path}
    SH
    chmod 0755, bin/"tokenstatmenu"
  end

  def caveats
    <<~EOS
      Run tokenstatmenu to open the menu bar app.
      Quit an existing TokenStatMenu before brew upgrade, then run tokenstatmenu again.
      Manual app copies and preferences are kept. No process is stopped automatically.
      Built locally from source with ad-hoc signing; this is not Apple notarization.
    EOS
  end

  test do
    app = libexec/"TokenStatMenu.app"
    expected = %w[
      Contents/Info.plist
      Contents/MacOS/TokenStatMenu
      Contents/Resources/AppIcon.icns
      Contents/_CodeSignature/CodeResources
    ]
    members = Dir.glob("#{app}/**/*", File::FNM_DOTMATCH)
    assert members.none? { |path| File.symlink?(path) }
    actual = members.select { |path| File.file?(path) }.map { |path| path.delete_prefix("#{app}/") }.sort
    assert_equal expected.sort, actual
    info = app/"Contents/Info.plist"
    plist = "/usr/libexec/PlistBuddy"
    assert_equal version.to_s, Utils.safe_popen_read(plist, "-c", "Print :CFBundleShortVersionString", info).strip
    assert_equal "7", Utils.safe_popen_read(plist, "-c", "Print :CFBundleVersion", info).strip
    assert_equal "d4ee8a1080b6a4d54ff1cbc0594aac924798a3ac",
                 Utils.safe_popen_read(plist, "-c", "Print :TokenStatSourceRevision", info).strip
    assert_equal "release-source", Utils.safe_popen_read(plist, "-c", "Print :TokenStatSourceState", info).strip
    assert_equal "edfc3aa520be04f987cc6c8a273995f57bafca68603d89f8265c640d0009e980", Digest::SHA256.file(app/"Contents/Resources/AppIcon.icns").hexdigest
    architecture = Hardware::CPU.arm? ? "arm64" : "x86_64"
    actual_arch = Utils.safe_popen_read("/usr/bin/lipo", "-archs", app/"Contents/MacOS/TokenStatMenu").strip
    assert_equal architecture, actual_arch
    system "/usr/bin/codesign", "--verify", "--deep", "--strict", app
    launcher = bin/"tokenstatmenu"
    app_path = (opt_libexec/"TokenStatMenu.app").to_s.shellescape
    expected_launcher = <<~SH
      #!/bin/sh
      if [ "$#" -ne 0 ]; then
        echo "Usage: tokenstatmenu" >&2
        exit 64
      fi
      exec /usr/bin/open #{app_path}
    SH
    assert_equal expected_launcher, launcher.read
    assert_match "Usage: tokenstatmenu", shell_output("#{launcher.to_s.shellescape} --not-supported 2>&1", 64)
  end
end
