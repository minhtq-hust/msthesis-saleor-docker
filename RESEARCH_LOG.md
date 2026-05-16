## [2026-04-28] - Experiment: Đổi tên nhánh master thành main
- **Hypothesis:** Có thể đổi tên nhánh chính từ master sang main và đồng bộ với remote GitHub.
- **Technical Implementation:** Sử dụng `git branch -m main`, sau đó `git push -u origin main` và xóa nhánh master trên remote.
- **Outcome:** SUCCESS
- **AI Analysis:** Việc đổi tên nhánh thành công sau khi xử lý xung đột lịch sử phân nhánh bằng rebase. Đây là thao tác chuẩn để đồng bộ hóa nhánh chính với quy chuẩn hiện đại.
- **Key Lesson:** Khi đổi tên nhánh chính, cần chú ý lịch sử phân nhánh giữa local và remote để tránh lỗi push bị từ chối.

