import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: link
    required property var theme
    property string label: ""
    property int branches: 1
    property bool merge: false
    property Item fromItem: null
    property Item toItem: null
    property real branchWidth: width
    Layout.fillWidth: true
    implicitHeight: label ? 60 : 36
    Canvas {
        id: drawing
        anchors.fill: parent
        onWidthChanged: requestPaint()
        onHeightChanged: requestPaint()
        Connections { target: link; function onBranchesChanged() { drawing.requestPaint() } function onMergeChanged() { drawing.requestPaint() } }
        Connections { target: link.theme; function onAccentChanged() { drawing.requestPaint() } }
        onPaint: {
            const c = getContext("2d")
            c.reset(); c.strokeStyle = link.theme.accent; c.fillStyle = link.theme.accent; c.lineWidth = 2
            const top = link.label ? 25 : 3, bottom = height - 5, mid = (top + bottom) / 2
            const from = link.fromItem ? link.fromItem.mapToItem(drawing,link.fromItem.width/2,0).x : width/2
            const to = link.toItem ? link.toItem.mapToItem(drawing,link.toItem.width/2,0).x : width/2
            function line(x1,y1,x2,y2) { c.moveTo(x1,y1); c.lineTo(x2,y2) }
            function head(x,y) { line(x-5,y-6,x,y); line(x,y,x+5,y-6) }
            c.beginPath()
            if (link.branches < 2) { line(from,top,from,mid); line(from,mid,to,mid); line(to,mid,to,bottom); head(to,bottom) }
            else {
                if (link.merge) {
                    line(width/2,mid,to,mid); line(to,mid,to,bottom)
                } else {
                    line(from,top,from,mid); line(from,mid,width/2,mid)
                }
                const left = width/2-link.branchWidth/4, right = width/2+link.branchWidth/4
                line(left,mid,right,mid)
                for (let x of [left,right]) {
                    line(x,link.merge ? top : mid,x,link.merge ? mid : bottom)
                    if (!link.merge) head(x,bottom)
                }
                if (link.merge) head(to,bottom)
            }
            c.stroke()
        }
    }
    Label { anchors.top: parent.top; anchors.horizontalCenter: parent.horizontalCenter; text: link.label; color: link.theme.muted; font.pixelSize: 11 }
}
