"""PornHub's browser challenge is recognised and solved without PhantomJS."""

from __future__ import annotations

import re
import shutil

import pytest

from yt_dlp_plugins.extractor.pornhub import (
    PornHubMediahubIE,
    challenge_script,
    is_challenge,
    run_with_deno,
    solve_challenge,
)

# The interstitial as served on 2026-10-01, byte for byte apart from whitespace.
CHALLENGE = """<html><head><script type="text/javascript"><!--
function leastFactor(n) {
 if (isNaN(n) || !isFinite(n)) return NaN;
 if (typeof phantom !== 'undefined') return 'phantom';
 if (typeof module !== 'undefined' && module.exports) return 'node';
 if (n==0) return 0;
 if (n%1 || n*n<2) return 1;
 if (n%2==0) return 2;
 if (n%3==0) return 3;
 if (n%5==0) return 5;
 var m=Math.sqrt(n);
 for (var i=7;i<=m;i+=30) {
  if (n%i==0)      return i;
  if (n%(i+4)==0)  return i+4;
  if (n%(i+6)==0)  return i+6;
  if (n%(i+10)==0) return i+10;
  if (n%(i+12)==0) return i+12;
  if (n%(i+16)==0) return i+16;
  if (n%(i+22)==0) return i+22;
  if (n%(i+24)==0) return i+24;
 }
 return n;
}
function go() {
 var p=1801954818029; var s=780217748; var n;
if ((s >> 8) & 1)/*
else p-=
*/p+=/* 120886108*
*/341427132*/*
*13;
*/11;
else  p-=/*
else p-=
*/443282696* 9;\tif ((s >> 14) & 1)/*
p+= */p+=/*
else p-=
*/118335511*17;/* 120886108*
*/else  p-=
46540108*15;\tif ((s >> 2) & 1)/*
else p-=
*/p+=/*
else p-=
*/638236383*/*
*13;
*/5;/*
else p-=
*/else /* 120886108*
*/p-=\t736640352* 3;
if ((s >> 2) & 1)\tp+=692048970*/*
else p-=
*/5;/*
*13;
*/else /* 120886108*
*/p-=/*
else p-=
*/674932735*\t3;/*
p+= */if ((s >> 15) & 1)/* 120886108*
*/p+=/*
p+= */169937885* 16;else p-=/*
*13;
*/880746*\t16; p-=10865453241;
 n=leastFactor(p);
{ document.cookie="KEY="+n+"*"+p/n+":"+s+":2237652997:1;path=/;";
  document.location.reload(true); }
}
//--></script></head>
<body onload="go()">
Loading...
</body>
</html>
"""

VIDEO_PAGE = (
    "<html><head><title>Some video - Pornhub.com</title></head><body>flashvars</body></html>"
)


class TestRecognition:
    def test_the_interstitial_is_a_challenge(self) -> None:
        assert is_challenge(CHALLENGE)

    def test_a_video_page_is_not(self) -> None:
        assert not is_challenge(VIDEO_PAGE)

    def test_the_script_is_extracted_without_its_comment_wrapper(self) -> None:
        script = challenge_script(CHALLENGE)

        assert script is not None
        assert script.startswith("function leastFactor(n)")
        assert "<!--" not in script
        assert "//-->" not in script

    def test_a_page_without_the_function_has_no_script(self) -> None:
        assert challenge_script(VIDEO_PAGE) is None


class TestSolving:
    def test_the_cookie_the_script_sets_is_returned(self) -> None:
        seen: list[str] = []

        def fake_runner(code: str) -> str:
            seen.append(code)
            return "KEY=12*34:56:78:1;path=/;\n"

        assert solve_challenge(CHALLENGE, fake_runner) == ("KEY", "12*34:56:78:1")
        assert "function go()" in seen[0]
        assert "globalThis.document" in seen[0], "the script needs a document to write to"
        assert seen[0].rstrip().endswith("console.log(document.cookie);")

    def test_no_cookie_means_no_answer(self) -> None:
        assert solve_challenge(CHALLENGE, lambda _code: "") is None
        assert solve_challenge(VIDEO_PAGE, lambda _code: "KEY=1") is None

    @pytest.mark.skipif(shutil.which("deno") is None, reason="deno is only in the image")
    def test_deno_solves_the_real_interstitial(self) -> None:
        """Runs where deno exists (the container); the cookie has the shape the site checks."""
        solved = solve_challenge(CHALLENGE, run_with_deno)

        assert solved is not None
        name, value = solved
        assert name == "KEY"
        assert re.fullmatch(r"\d+\*\d+:780217748:2237652997:1", value), value


class TestUrls:
    def test_the_built_in_links_are_taken_here(self) -> None:
        assert PornHubMediahubIE.suitable("https://www.pornhub.com/view_video.php?viewkey=648719015")
        assert PornHubMediahubIE.IE_NAME == "pornhub:mediahub"
