import QtQuick
import QtQuick.Layouts

Item {
    id: sequence
    required property var theme
    default property alias cards: grid.data
    Layout.fillWidth: true
    implicitHeight: grid.implicitHeight
    GridLayout {
        id: grid
        columns: sequence.width >= 876 ? 3 : sequence.width >= 568 ? 2 : 1
        width: Math.min(sequence.width, columns * 260 + (columns - 1) * 48)
        anchors.horizontalCenter: parent.horizontalCenter
        columnSpacing: 48; rowSpacing: 48
        onImplicitHeightChanged: lines.requestPaint()
    }
    Canvas {
        id: lines; anchors.fill: parent; z: -1
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        onPaint: {
            const c = getContext("2d"); c.reset(); c.strokeStyle = sequence.theme.accent; c.lineWidth = 2
            const nodes = grid.children.filter(item => item.visible && item.width > 0)
            function point(item,x,y) { return item.mapToItem(lines,x,y) }
            function arrow(x,y,horizontal) {
                if (horizontal) { c.moveTo(x-6,y-5); c.lineTo(x,y); c.lineTo(x-6,y+5) }
                else { c.moveTo(x-5,y-6); c.lineTo(x,y); c.lineTo(x+5,y-6) }
            }
            c.beginPath()
            for (let i=0; i<nodes.length-1; i++) {
                const a=nodes[i], b=nodes[i+1]
                if (b.x>a.x) {
                    const start=point(a,a.width,a.height/2), end=point(b,0,b.height/2)
                    c.moveTo(start.x+3,start.y); c.lineTo(end.x-5,start.y); arrow(end.x-5,start.y,true)
                } else {
                    const start=point(a,a.width/2,a.height), end=point(b,b.width/2,0), mid=(start.y+end.y)/2
                    c.moveTo(start.x,start.y+3); c.lineTo(start.x,mid); c.lineTo(end.x,mid); c.lineTo(end.x,end.y-5); arrow(end.x,end.y-5,false)
                }
            }
            c.stroke()
        }
        Connections { target: sequence.theme; function onAccentChanged() { lines.requestPaint() } }
    }
}
