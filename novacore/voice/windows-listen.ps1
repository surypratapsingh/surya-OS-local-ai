# NOVA listener for Windows (work order N2). Built in and offline: System.Speech,
# the desktop speech recogniser that ships with Windows. Prints each recognised
# utterance on its own line; the audio stays inside this process.
# UNVERIFIED: not run in the session that wrote it (no Windows host there).
Add-Type -AssemblyName System.Speech
$recognizer = New-Object System.Speech.Recognition.SpeechRecognitionEngine
$recognizer.LoadGrammar((New-Object System.Speech.Recognition.DictationGrammar))
$recognizer.SetInputToDefaultAudioDevice()
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
while ($true) {
    $result = $recognizer.Recognize([TimeSpan]::FromSeconds(30))
    if ($result -ne $null -and $result.Text) {
        [Console]::Out.WriteLine($result.Text)
        [Console]::Out.Flush()
    }
}
