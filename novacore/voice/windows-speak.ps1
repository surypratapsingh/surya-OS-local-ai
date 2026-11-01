# NOVA speaker for Windows (work order N2). Built in and offline: System.Speech.
# Reads one reply on stdin and says it.
# UNVERIFIED: not run in the session that wrote it (no Windows host there).
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
$text = [Console]::In.ReadToEnd()
if ($text.Trim().Length -gt 0) { $synth.Speak($text) }
