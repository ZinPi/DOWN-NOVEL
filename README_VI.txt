TRUYỆN NHẬT TXT TOOL v0.9.4
Narou + Narou R18 + Kakuyomu + GameLive + Auto Web Analyzer
=============================================================

MỤC ĐÍCH
--------
Tool Windows chạy cục bộ để:
- Dán URL truyện và tự nhận diện nguồn.
- Với 4 website đã biết: dùng adapter chuyên dụng.
- Với website lạ: tự phân tích HTML/DOM, tìm mục lục + vùng nội dung, test 3 episode rồi mới cho tải.
- Chọn chính xác episode cần tải: 1-32, 50, 1,3,10-25...
- Xuất mỗi episode, ghép theo chương lớn, hoặc ghép tất cả thành một TXT.
- Cache episode đã tải để chạy lại nhanh hơn.
- Xuất báo cáo TSV/TXT về số tập và số ký tự.

WEBSITE HỖ TRỢ CHUYÊN DỤNG
---------------------------
1. Narou thường
   https://ncode.syosetu.com/<ncode>/

2. Narou R18 / novel18
   https://novel18.syosetu.com/<ncode>/

3. Kakuyomu
   https://kakuyomu.jp/works/<work-id>

4. GameLive Studio
   https://gamelive.online/vi/app/stories/<story-id>

5. Website khác
   Chuyển sang AUTO WEB ANALYZER. Không bảo đảm mọi website đều lấy được.

AUTO WEB ANALYZER v0.9.4
----------------------
Khi URL không thuộc 4 adapter trên, tool chạy pipeline:

  HTTP/HTML
    -> metadata / JSON-LD
    -> phân cụm link episode
    -> nếu HTML chưa đủ: Chrome/Playwright render JavaScript
    -> thử mục lục có Next rõ ràng (cả sau khi Chrome render)
    -> tải thử episode đầu / giữa / cuối
    -> tìm CSS selector nội dung ổn định
    -> nếu PASS: lưu Site Profile
    -> cho phép tải TXT

Tool KHÔNG tải hàng loạt nếu mới chỉ đoán được selector. Ba episode mẫu phải PASS trước.

SITE PROFILE
------------
Profile Auto Web được lưu theo domain tại:
  %LOCALAPPDATA%\Truyen_Nhat_TXT_Tool\AutoWeb\profiles

Profile gồm các dữ liệu như:
- domain
- body selector
- có cần browser JavaScript hay không
- group URL episode
- confidence

Lần sau cùng domain, tool thử profile cũ trên 3 episode. Nếu profile cũ không còn đúng, tool tự phân tích lại.

BÁO CÁO AUTO WEB
----------------
Khi PASS hoặc FAIL, báo cáo nằm tại:
  %LOCALAPPDATA%\Truyen_Nhat_TXT_Tool\AutoWeb\reports\<domain>\<thời-gian>

Có thể gồm:
- AUTO_ANALYSIS_REPORT.json
- AUTO_ANALYSIS_REPORT.txt
- HTML trang mục lục / episode mẫu
- screenshot khi phải dùng Chrome
- network_candidates.json khi bắt được XHR/fetch ứng viên

Trong giao diện có nút “Mở báo cáo Auto”.

TRƯỜNG HỢP TOOL CHỦ ĐỘNG BÁO KHÔNG LẤY ĐƯỢC
----------------------------------------------
- HTTP 401/403/429: dừng an toàn, không cố vượt.
- CAPTCHA / Verify human / Turnstile / reCAPTCHA: BLOCKED_BY_ANTI_BOT.
- Paywall / nội dung không có quyền truy cập: không vượt.
- SPA chỉ trả nội dung qua API nội bộ nhưng chưa suy ra endpoint tin cậy:
  DYNAMIC_APP_API_NOT_AUTO_RESOLVED.
- Không tìm được vùng nội dung ổn định trên 3 episode:
  TEXT_NOT_RELIABLY_DETECTED / LOW_CONFIDENCE_PROFILE.
- Nội dung chỉ có ảnh/canvas: bản này chưa OCR.
- Nội dung API bị mã hóa/obfuscate: cần adapter riêng.

CÁCH CHẠY LẦN ĐẦU
-----------------
1. Cài Python 3.11 trở lên từ python.org.
2. Khi cài Python tick “Add python.exe to PATH”.
3. Giải nén ZIP.
4. Nhấp đúp INSTALL_AND_RUN.bat.
5. Những lần sau dùng RUN.bat.

CÁCH DÙNG NHANH
---------------
1. Dán URL tác phẩm / trang mục lục.
2. Bấm PHÂN TÍCH.
3. Nếu site đã biết: parser chuyên dụng chạy như trước.
4. Nếu site lạ: chờ Auto Web phân tích và test 3 episode.
5. Nếu PASS: kiểm tra tên truyện / tác giả / số episode.
6. Nhập episode cần lấy.

Ví dụ:
  1-32        = episode 1 đến 32
  50          = chỉ episode 50
  1,3,10-25   = episode 1, 3, và 10 đến 25
  90~120      = episode 90 đến 120

7. Bấm Áp dụng.
8. Chọn kiểu xuất TXT.
9. Bấm TẢI + GHÉP TXT.

CÁCH XUẤT TXT
-------------
- Mỗi chương lớn = 1 TXT.
- Mỗi episode = 1 TXT.
- Gộp tất cả = 1 TXT.

Với Auto Web chưa xác định được chương lớn, toàn bộ episode nằm trong một chương “Tự động phát hiện”.

CACHE
-----
Mỗi episode tải thành công có thể được lưu trong .cache/.
Nếu dừng hoặc mạng lỗi, chạy lại với “Dùng cache” để tiếp tục.

NAROU / NAROU R18
------------------
- Narou R18 tự đặt cookie over18=yes.
- Không cần Chrome cho age-gate hiện tại.
- 前書き / 後書き áp dụng cho Narou + Narou R18.

KAKUYOMU
--------
- Ưu tiên __NEXT_DATA__ / __APOLLO_STATE__.
- Có fallback HTML.
- 本文 lấy từ widget-episodeBody / js-episode-body.

GAMELIVE
--------
- Dùng Playwright + Chrome profile thật.
- “Mở / đăng nhập” để người dùng tự đăng nhập.
- “Dò API” lưu XHR/fetch phục vụ adapter FAST.

TẠO FILE EXE
------------
1. Chạy TAO_FILE_EXE.bat.
2. Chờ PyInstaller hoàn tất.
3. File nằm tại dist\Truyen_Nhat_TXT_Tool.exe

KIỂM TRA v0.9.3
-------------
- Python compile: PASS
- test_core.py: PASS
- Narou / Narou R18 / Kakuyomu / GameLive cũ: PASS
- Domain lạ -> Auto Web: PASS
- Auto Web synthetic: tìm mục lục, test 3 episode, tìm body selector, lưu profile/report: PASS

LƯU Ý
-----
- Tải episode tuần tự, không spam request song song.
- Delay mặc định 2.2 giây/request.
- Auto Analyzer ưu tiên dừng an toàn khi không đủ tin cậy.
- Báo cáo/debug được tạo để có thể viết adapter riêng cho site mà generic analyzer chưa xử lý được.


CẬP NHẬT v0.9.3 STABILITY
--------------------------
- Mục lục phân trang: theo Next đến hết, tối đa 120 trang; nếu vẫn còn Next ở giới hạn thì FAIL thay vì PASS thiếu.
- Nếu trang mục lục kế tiếp lỗi hoặc pattern episode biến mất bất thường, dừng và xuất báo cáo.
- Chrome/Playwright tự cuộn tới khi DOM ổn định để hỗ trợ infinite/virtual scroll.
- Thêm Novel Likeness Gate chống nhận nhầm blog/news/archive thành truyện.
- Body selector ưu tiên vùng nhỏ, sạch; loại comment/menu/share/recommend/related khi wrapper quá rộng.
- Site Profile tăng version 2; profile cũ không tương thích sẽ bị bỏ và tự học lại.
- INSTALL_AND_RUN đồng bộ version 0.9.1.


CẬP NHẬT QUAN TRỌNG v0.9.4 (2026-10-23)
----------------------------------------
1. Hai ô lưu kết quả tách biệt:
   - Lưu tại: chọn THƯ MỤC GỐC (ví dụ D:\Truyen).
   - Tên thư mục: TỰ GHI MỘT TÊN NGẮN (ví dụ TRUYEN_001, TEST_ABC).
   - Để trống ô Tên thư mục: tool tự lấy MÃ TRUYỆN, KHÔNG lấy tiêu đề dài.
   - Tên có ký tự cấm Windows được tự chuyển thành ký tự '_' hoặc làm sạch.
   - Tên tự nhập quá dài được giới hạn còn tối đa 48 đơn vị UTF-16.

2. Cấu trúc đầu ra:
   <Lưu tại>/<Tên thư mục>/
     .cache/<Mã truyện>/                (cache từng episode, tái sử dụng giữa các lần chạy)
     RUN_YYYYMMDD_HHMMSS/              (mỗi lần xuất 1 thư mục kết quả mới)
       _TRANG_THAI_HOAN_TAT.txt         (chỉ có nếu xuất và QA local thành công)
       TAT_CA_EPISODE_DA_CHON.txt       (khi chọn gộp)
       episodes/                        (khi chọn từng episode)
       chapters/                        (khi chọn từng chương)
       00_THONG_TIN_VA_TONG_KET.txt
       01_DANH_SACH_EPISODE.tsv
       02_BAO_CAO_CHUONG.tsv
       03_QA_KIEM_TRA_XUAT.json

   Tên TXT được giới hạn riêng, không để tiêu đề tác phẩm dài chiếm đường dẫn.
   Nếu hai lần tải diễn ra cùng giây, tên RUN tiếp theo tự có hậu tố _001, _002...
   Nếu tác vụ dừng/lỗi, vẫn còn file _TRANG_THAI_DANG_TAI.txt để cảnh báo KHÔNG HOÀN CHỈNH.
   TUYỆT ĐỐI không xem thư mục có marker này là FINAL.

3. Sửa lỗi:
   - Khi mục lục chỉ lộ sau JavaScript: chạy phân trang Next SAU bước browser fallback.
   - Trang Next không có episode hợp lệ => FAIL, không âm thầm báo đủ.
   - Giữ lời thoại trong <blockquote>, xuống dòng <br>, vẫn bỏ ruby <rt>/<rp>.
   - Đổi URL hoặc phân tích lại => khóa kết quả cũ, tải bị vô hiệu cho tới khi phân tích thành công.
   - Episode download được kiểm nội dung (>=10 ký tự, đúng URL); cache có SHA-256, version.
   - Có đối chiếu số episode website công bố khi con số có sẵn.
   - Mỗi lần tải ghi manifest + SHA-256 mỗi thân truyện để QA cục bộ.
   - Không ghi nội dung phản hồi XHR/fetch vào network_candidates.json theo mặc định.
   - Không ghi đè ảnh/HTML giữa các lần gọi browser trong cùng một báo cáo.

4. Cài đặt:
   - Windows 10/11, Python 3.11+, Chrome/Edge (nếu website yêu cầu browser).
   - Chạy INSTALL_AND_RUN.bat lần đầu: tự tạo .venv để cài thư viện cách ly.
   - Những lần tiếp theo chạy RUN.bat.
   - Nếu không có Python / thư viện / trình duyệt phù hợp, tool có thể không chạy.
   - ZIP chứa MÃ NGUỒN, không có EXE biên dịch sẵn. TAO_FILE_EXE.bat tùy chọn.

5. Giới hạn phải hiểu đúng:
   - Báo cáo 03_QA là PASS cho dữ liệu XUẤT CỤC BỘ, KHÔNG chứng minh website
     đã cho thấy 100% số episode hoặc nội dung nguyên bản.
   - Auto Web chỉ quét chắc chắn các liên kết Next hiện rõ trong DOM khi đọc.
     Website dùng infinite scroll / virtual list / nút Next không có href có thể còn thiếu.
   - Có thể có chương cực ngắn hợp lệ dưới 10 ký tự: tool sẽ chủ động báo lỗi để
     người dùng kiểm tra thay vì chấp nhận dữ liệu nghi vấn.
   - HTML/screenshot chẩn đoán vẫn có thể chứa thông tin riêng tư; KHÔNG chia sẻ
     thư mục debug/Auto Web report trước khi rà soát và che dữ liệu.
   - Chưa kiểm thử online các website thật hay kiểm chứng cài đặt native Windows.

6. Tự kiểm tra kỹ thuật (ngoại tuyến):
   python test_core.py
   python test_regression_v094.py
   Bộ regression có test giao diện: chạy python test_regression_v094.py --gui
   trên môi trường có màn hình / máy chủ X phù hợp.
