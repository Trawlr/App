# Note: this download helper was AI generated. Milage may vary.

$base      = "https://TRAWLR.INSTANCE/api/v1"
$token     = "APIKEY"
$channelId = "100"                                   # trawlr source ID
$outDir    = "C:\trawlr"
$headers   = @{ Authorization = "Bearer $token" }

New-Item -ItemType Directory -Force $outDir | Out-Null

# Only images downloaded in the last 30 days will be downloaded
$since = [DateTime]::UtcNow.AddDays(-30).ToString("yyyy-MM-ddTHH:mm:ssZ")
$enc   = [uri]::EscapeDataString($since)
Write-Host "Downloading images for channel $channelId since $since"

$ok = 0; $skipped = 0
$seen = [System.Collections.Generic.HashSet[int]]::new()
$pageNum = 1
do {
    $page = Invoke-RestMethod -Headers $headers `
        -Uri "$base/files?channel=$channelId&fileType=photo&downloadedAfter=$enc&page=$pageNum"

    foreach ($f in $page.results) {
        $dlId = $f.id
        if (-not $f.filePath) {
            $detail = Invoke-RestMethod -Headers $headers -Uri "$base/files/$($f.id)"
            if ($detail.originalFile) {
                $dlId = $detail.originalFile
            } else {
                $skipped++
                Write-Warning "Skipped $($f.id): no file on storage (deletedFromDisk=$($detail.deletedFromDisk))"
                continue
            }
        }

        if (-not $seen.Add([int]$dlId)) { continue } 

        $dest = Join-Path $outDir "$dlId.jpg"
        try {
            Invoke-WebRequest -Headers $headers -Uri "$base/files/$dlId/download" -OutFile $dest -ErrorAction Stop
            $ok++
            Write-Host "Downloaded file $dlId ($dest)"
        } catch {
            $skipped++
            Write-Warning "Skipped file $dlId : $($_.Exception.Message)"
            if (Test-Path $dest) { Remove-Item $dest -Force }
        }
    }
    $pageNum++
} while ($page.next)

Write-Host "Done. $ok downloaded, $skipped skipped -> $outDir"
