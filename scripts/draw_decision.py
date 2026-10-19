"""Render container adoption considerations as three code-drawn pictograms."""
from draw_architecture import Canvas, OUT, INK, MUTED, BLUE, GREEN


def circle(c, x, y, radius, color):
    c.svg.append(f'<circle cx="{x}" cy="{y}" r="{radius}" fill="white" stroke="{color}" stroke-width="2.4"/>')
    c.draw.ellipse(((x-radius)*2,(y-radius)*2,(x+radius)*2,(y+radius)*2),fill='white',outline=color,width=5)


def people(c, x, y, color):
    for dx in (-24,24):
        circle(c,x+dx,y-10,9,color)
        c.rect(x+dx-13,y+3,26,25,stroke=color,radius=10)
    circle(c,x,y-15,12,color)
    c.rect(x-17,y+2,34,34,stroke=color,radius=12)


def policy(c, x, y, color):
    c.rect(x-26,y-32,52,68,stroke=color,radius=7)
    for dy in (-15,3,21):
        c.line([(x-17,y+dy),(x-13,y+dy+4),(x-7,y+dy-4)],color=color)
        c.line([(x+1,y+dy),(x+17,y+dy)],color=color)


def servers(c, x, y, color):
    for dy in (-22,2,26):
        c.rect(x-32,y+dy-10,64,19,stroke=color,radius=4)
        circle(c,x-21,y+dy,2,color)
        c.line([(x-9,y+dy),(x+21,y+dy)],color=color)


def main():
    c=Canvas(1200,420)
    c.svg[1]='<title>컨테이너 전환 시 검토할 투자와 운영 변화</title>'
    c.text(32,38,'컨테이너 전환 시 검토할 투자',27,bold=True,anchor='start')
    c.text(32,79,'기술 검증과 함께 인력, 운영 정책, 인프라의 준비 범위를 검토',17,MUTED,anchor='start')
    cards=[('인적 자원',people,BLUE,'운영 역량 · 교육 · 담당자 확보','클러스터 운영 · 장애 대응'),
           ('정책·운영 체계',policy,GREEN,'권한 · 보안 · 배포 승인 정책','모니터링 · 백업 · 복구 절차'),
           ('물적 자원',servers,BLUE,'서버 · 저장소 · 네트워크','레지스트리 · 모니터링 인프라')]
    for i,(title,icon,color,line1,line2) in enumerate(cards):
        x=32+i*392;center=x+176
        c.rect(x,110,352,250)
        icon(c,center,168,color)
        c.text(center,236,title,23,color,bold=True)
        c.text(center,283,line1,17,INK)
        c.text(center,316,line2,17,MUTED)
    c.text(32,393,'검토 항목을 정리한 그림이며, 투자 규모와 비용을 정량 측정한 결과는 아님',14,MUTED,anchor='start')
    c.save('container-adoption')
    print(OUT)


if __name__=='__main__':
    main()
