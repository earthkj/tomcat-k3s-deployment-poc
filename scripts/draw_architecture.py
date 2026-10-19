"""Render aligned architecture and rollout comparisons as editable SVG and PNG."""
from pathlib import Path
from xml.sax.saxutils import escape
from math import hypot
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/assets'
INK = '#172b42'
MUTED = '#52657a'
LINE = '#64788c'
BLUE = '#2870ae'
GREEN = '#147d68'
PURPLE = '#7651a8'
BORDER = '#d6e0ea'


class Canvas:
    def __init__(self, width, height):
        self.width, self.height = width, height
        self.svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img">', '<title>배포 아키텍처 비교</title>', '<rect width="100%" height="100%" fill="#f5f8fc"/>']
        self.image = Image.new('RGB', (width * 2, height * 2), '#f5f8fc')
        self.draw = ImageDraw.Draw(self.image)
        regular = Path('C:/Windows/Fonts/malgun.ttf')
        bold = Path('C:/Windows/Fonts/malgunbd.ttf')
        candidates = [(regular, bold), (Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'), Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'))]
        self.fonts = next((pair for pair in candidates if all(p.exists() for p in pair)), None)
        if self.fonts is None:
            raise RuntimeError('Install Malgun Gothic (Windows) or Noto Sans CJK fonts to render Korean text.')

    def rect(self, x, y, w, h, fill='white', stroke=BORDER, radius=12, dashed=False):
        dash = ' stroke-dasharray="6 5"' if dashed else ''
        self.svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="1.4"{dash}/>')
        bounds = (x * 2, y * 2, (x + w) * 2, (y + h) * 2)
        self.draw.rounded_rectangle(bounds, radius=radius * 2, fill=fill, outline=None if dashed else stroke, width=3)
        if dashed:
            for a in range(int(x + 8), int(x + w - 8), 11):
                for b in (y, y + h): self.draw.line((a * 2, b * 2, min(a + 6, x + w - 8) * 2, b * 2), fill=stroke, width=3)
            for b in range(int(y + 8), int(y + h - 8), 11):
                for a in (x, x + w): self.draw.line((a * 2, b * 2, a * 2, min(b + 6, y + h - 8) * 2), fill=stroke, width=3)

    def text(self, x, y, value, size=18, color=INK, bold=False, anchor='middle'):
        self.svg.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}" dominant-baseline="middle" font-family="Malgun Gothic,Noto Sans CJK KR,sans-serif" font-size="{size}" font-weight="{700 if bold else 400}" fill="{color}">{escape(value)}</text>')
        font = ImageFont.truetype(str(self.fonts[int(bold)]), size * 2)
        self.draw.text((x * 2, y * 2), value, font=font, fill=color, anchor='mm' if anchor == 'middle' else 'rm' if anchor == 'end' else 'lm')

    def line(self, points, arrow=False, color=LINE, width=2, dashed=False):
        coords = ' '.join(f'{x},{y}' for x, y in points)
        dash = ' stroke-dasharray="7 5"' if dashed else ''
        self.svg.append(f'<polyline{dash} points="{coords}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linejoin="round" stroke-linecap="round"/>')
        if dashed:
            for (a, b), (x, y) in zip(points, points[1:]):
                distance = hypot(x-a, y-b)
                for offset in range(0, int(distance), 12):
                    end = min(offset+7, distance)
                    self.draw.line(((a+(x-a)*offset/distance)*2, (b+(y-b)*offset/distance)*2, (a+(x-a)*end/distance)*2, (b+(y-b)*end/distance)*2), fill=color, width=width*2)
        else:
            self.draw.line([(x * 2, y * 2) for x, y in points], fill=color, width=width * 2, joint='curve')
        if arrow:
            (a, b), (x, y) = points[-2:]
            length = hypot(x-a, y-b)
            dx, dy = (x-a)/length, (y-b)/length
            triangle = [(x,y), (x-8*dx+5*dy,y-8*dy-5*dx), (x-8*dx-5*dy,y-8*dy+5*dx)]
            self.svg.append('<polygon points="' + ' '.join(f'{a},{b}' for a, b in triangle) + f'" fill="{color}"/>')
            self.draw.polygon([(a * 2, b * 2) for a, b in triangle], fill=color)

    def box(self, x, y, w, h, title, detail='', accent=False):
        self.rect(x, y, w, h, fill='#edf5fd' if accent else 'white', stroke='#9ebfdd' if accent else BORDER)
        self.text(x + w / 2, y + (h / 2 if not detail else h / 2 - 11), title, 17, bold=True)
        if detail: self.text(x + w / 2, y + h / 2 + 14, detail, 14, MUTED)

    def save(self, name):
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / (name + '.svg')).write_text('\n'.join(self.svg) + '\n</svg>\n', encoding='utf-8')
        self.image.save(OUT / (name + '.png'), optimize=True)


def topology():
    c = Canvas(1540, 1035)
    c.text(32, 40, '요청 경로와 배포 제어', 30, bold=True, anchor='start')
    c.text(32, 82, '기존 계층 배치 유지 · 요청 경로, JK Status 제어, Pod 교체 관계를 선 종류로 구분', 18, MUTED, anchor='start')
    names = [('Tomcat 순차 배포', '(현행)'), ('k3s 컨테이너 롤링', ''), ('Tomcat 롤링', '추가 인스턴스 방식'), ('Tomcat 롤링', '기존 인스턴스 방식')]
    for label, y in [('요청 발생', 265), ('진입점', 359), ('웹 서버', 465), ('서비스 라우팅', 562), ('애플리케이션', 687), ('데이터베이스', 866)]:
        c.text(24, y, label, 15, MUTED, anchor='start')
    for i, (title, detail) in enumerate(names):
        x = 166 + i * 338
        center = x + 156
        c.rect(x, 116, 312, 804, stroke='#9bcabd' if i == 3 else BORDER)
        c.text(center, 151, title, 21, bold=True)
        c.text(center, 181, detail or 'Deployment · RollingUpdate', 16, MUTED)
        if i == 3: c.text(center, 207, '최종 제안', 14, GREEN, bold=True)
        c.rect(x + 10, 620, 292, 178, fill='#f8fafc')
        c.text(center, 638, 'Tomcat Pod ×2' if i == 1 else 'Tomcat ×2', 14, MUTED)
        if i == 1:
            c.rect(x + 8, 518, 296, 286, fill='#eff6fd', stroke='#9ebfdd')
            c.text(x + 21, 536, 'k3s · 단일 노드', 13, BLUE, bold=True, anchor='start')
            c.rect(x + 10, 620, 292, 178, fill='#f8fafc')
            c.text(center, 638, 'Tomcat Pod ×2', 14, MUTED)
        c.box(x + 36, 240, 240, 50, '부하 발생기', '100 RPS')
        c.box(x + 36, 330, 240, 58, 'HAProxy', 'Round-robin')
        if i in (2, 3):
            c.rect(x + 36, 430, 240, 70)
            c.text(center, 449, 'Apache httpd ×2', 17, bold=True)
            c.text(center, 470, 'mod_jk · AJP', 14, MUTED)
            c.text(center, 490, 'JK Status 관리 API', 12, GREEN, bold=True)
            c.rect(x + 174, 533, 122, 62, fill='#eff8f4', stroke='#9bcabd')
            c.text(x + 235, 551, '배포 스크립트', 13, GREEN, bold=True)
            c.text(x + 235, 576, '유입 제외 · 복귀', 12, MUTED)
            c.line([(x + 235, 533), (x + 235, 500)], arrow=True, color=GREEN, dashed=True)
        else:
            c.box(x + 36, 430, 240, 70, 'Apache httpd ×2', 'mod_jk · AJP')
        c.line([(center, 290), (center, 330)], arrow=True)
        c.line([(center, 388), (center, 430)], arrow=True)
        if i == 1:
            c.box(x + 36, 550, 240, 52, 'Service (NodePort)', accent=True)
            c.line([(center, 500), (center, 550)], arrow=True)
            c.line([(center, 602), (center, 630)])
        else:
            c.line([(center, 500), (center, 630)])
            c.text(center - 12 if i in (2, 3) else center + 12, 565, 'AJP', 13, MUTED, anchor='end' if i in (2, 3) else 'start')
        c.line([(x + 84, 630), (x + 228, 630)])
        for j in (0, 1):
            bx = x + 20 + j * 144
            c.line([(bx + 64, 630), (bx + 64, 656)], arrow=True)
            c.box(bx, 656, 128, 60, 'Tomcat' if i != 1 else 'Pod ' + ('A' if j == 0 else 'B') + ' · v1', 'Tomcat' if i == 1 else '서비스 중')
        if i == 2:
            for j in (0, 1):
                bx = x + 20 + j * 144
                c.rect(bx, 738, 128, 44, fill='#fafbfc', dashed=True)
                c.text(bx + 64, 752, '대기 Tomcat', 13, MUTED)
                c.text(bx + 64, 770, '미기동', 12, MUTED)
        elif i == 1:
            for j in (0, 1):
                bx = x + 20 + j * 144
                c.line([(bx + 64, 716), (bx + 64, 744)], arrow=True, color=PURPLE, dashed=True)
                c.rect(bx, 744, 128, 40, fill='#f4effa', stroke='#b8a3d5')
                c.text(bx + 64, 764, 'Pod ' + ('A′' if j == 0 else 'B′') + ' · v2', 14, PURPLE, bold=True)
        else:
            c.text(center, 764, '정상 상태: 요청 처리 인스턴스 2개', 13, MUTED)
        c.line([(center, 804 if i == 1 else 798), (center, 834)], arrow=True)
        c.box(x + 36, 834, 240, 62, 'PostgreSQL', '공통 읽기 전용 DB')
    c.text(32, 947, '실선: 요청 경로 · 초록 점선: JK Status 관리 요청 · 보라 점선: 기존 Pod → 새 Pod 교체 관계', 15, MUTED, anchor='start')
    c.text(32, 977, 'Pod A/B → A′/B′는 순차 교체 전후를 뜻함 · 4개 Pod 동시 서비스나 기존 Pod 내부 업데이트를 뜻하지 않음', 15, MUTED, anchor='start')
    c.text(32, 1007, '회색 점선 상자: 미기동 대기 Tomcat · DB: 동일한 공유 인스턴스 · 모든 비교군은 Windows/Docker에서 재현', 15, MUTED, anchor='start')
    c.save('architecture-topology')


def rollout():
    c = Canvas(1540, 1050)
    c.text(32, 40, '배포 단계 비교', 30, bold=True, anchor='start')
    c.text(32, 82, '왼쪽에서 오른쪽으로 진행 · 준비 확인: 구성별 readiness·API 검사 · 드레이닝: 처리 중 요청 완료 대기', 17, MUTED, anchor='start')
    cases = [
        ('Tomcat 순차 배포', '(현행)', [('대상 1 중지', '사전 유입 제외 없음'), ('목표 버전 기동', '컨테이너 재생성'), ('준비 상태 확인', '직접 readiness 확인'), ('대상 2 반복', '같은 순서로 교체'), ('앞단 응답 검증', '목표 버전 확인')], '교체 중 한 대가 서비스를 담당 · 재시도 이후 최종 클라이언트 응답을 측정'),
        ('k3s 컨테이너 롤링', '', [('새 Pod A′ 생성', 'A/B는 기존 v1 · 신규는 v2'), ('준비 상태 확인', 'readiness · 5초 준비 유지'), ('기존 Pod A 종료', 'A′ 준비 후 종료 시작'), ('Pod B → B′ 교체', '같은 절차 · 종료 중 공존'), ('롤아웃 완료 확인', 'A′/B′ v2 · A/B 종료')], '정상 Pod 2개 · 신규 Pod와 종료 중 Pod가 일시 공존 · 전체 Pod 수 3개 상한을 뜻하지 않음'),
        ('Tomcat 롤링', '추가 인스턴스 방식', [('대체 1대 준비', '기동 · readiness · API'), ('새 대상 유입 투입', 'JK Status · Active'), ('기존 1대 유입 제외', 'JK Status · 드레이닝'), ('기존 종료 후 반복', '다음 대체 인스턴스 준비'), ('앞단 응답 검증', '목표 버전 · 유입 대상')], '정상 실행 2대 · 배포 중 최대 3대 · 두 대를 한 그룹으로 동시 전환하지 않음'),
        ('Tomcat 롤링', '기존 인스턴스 · 최종 제안', [('기존 1대 유입 제외', 'JK Status · Stopped'), ('진행 요청 드레이닝', '양쪽 Apache busy=0 확인'), ('교체 · 준비 확인', '재생성 · readiness · API'), ('유입 복귀 후 반복', 'JK Status · Active'), ('앞단 응답 검증', '목표 버전 · 유입 대상')], '앱 인스턴스 2대 이내 · 교체 중 한 대가 전체 트래픽 처리 · 추가 장애 여유 감소'),
    ]
    for index, (name, detail, steps, note) in enumerate(cases):
        y = 145 + index * 211
        color = GREEN if index == 3 else BLUE
        c.rect(24, y, 1492, 188, fill='white', stroke='#9bcabd' if index == 3 else BORDER)
        c.text(151, y + 56, name, 20, bold=True)
        if detail: c.text(151, y + 88, detail, 16, color)
        for j, (title, body) in enumerate(steps):
            x = 300 + j * 240
            c.box(x, y + 35, 214, 86, title, body, accent=True)
            c.text(x + 16, y + 18, str(j + 1).zfill(2), 12, color, bold=True)
            if j < 4: c.line([(x + 214, y + 78), (x + 240, y + 78)], arrow=True, color=color)
        c.text(300, y + 155, note, 15, MUTED, anchor='start')
    c.text(32, 1010, '제어 조건: 두 Apache의 상태 변경은 순차 적용 · 드레인 확인은 busy=0 연속 2초 · 자동 롤백은 검증 범위 밖', 15, MUTED, anchor='start')
    c.save('architecture-rollout')


if __name__ == '__main__':
    topology()
    rollout()
    print(OUT)
