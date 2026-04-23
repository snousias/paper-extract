# Forwards to: python extract_publications.py  (optional; same as running that from .librarian)
# Optional: -Dir "path"  ->  --dir "path" for remaining args
param(
    [string] $Dir,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $RemainingArgs
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyArgs = [System.Collections.Generic.List[string]]::new()
if ($PSBoundParameters.ContainsKey("Dir") -and -not [string]::IsNullOrWhiteSpace($Dir)) {
    $pyArgs.Add("--dir") | Out-Null
    $pyArgs.Add($Dir) | Out-Null
}
foreach ($a in $RemainingArgs) { $pyArgs.Add($a) | Out-Null }
& python "$here\extract_publications.py" @pyArgs
